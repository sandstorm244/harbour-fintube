import QtQuick 2.0
import Sailfish.Silica 1.0

Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: false
    property bool loadingMore: false       // paging in the next page of results
    property bool hasMore: false           // another page is (probably) available
    property int pageSize: 15
    property int nextStart: 1              // 1-based index of the next page's first result
    property string statusText: ""
    property bool statusError: false
    property string searchKind: "video"   // "video" | "channel"
    property string lastQuery: ""
    property string queryText: ""          // live text of the search field (see suggestTimer)
    property var suggestions: []           // autocomplete terms while typing
    property bool suggestBlocked: false    // true after a submit → suppress suggestions until next keystroke
    property var seenIds: ({})             // dedup set across paged results (reset per query)

    // Search filters (video kind only) — values are yt `sp` codes; 0 = unset.
    property int fSort: 0    // 0 relevance · 1 rating · 2 upload date · 3 view count
    property int fDate: 0    // 0 any · 2 today · 3 week · 4 month · 5 year
    property int fDur: 0     // 0 any · 1 <4min · 2 >20min · 3 4-20min
    property bool filtersOpen: false
    readonly property bool filtersActive: fSort > 0 || fDate > 0 || fDur > 0
    function filterObj() { return { sort: page.fSort, date: page.fDate, dur: page.fDur } }
    // Re-run the current query when a filter changes (video kind only; no-op before a first search).
    function applyFilters() {
        if (page.searchKind !== "video" || page.lastQuery.length === 0)
            return
        page.statusText = ""
        page.hasMore = false
        page.nextStart = 1
        page.loadResults(1)
    }

    function runSearch(q) {
        if (!q || q.length === 0)
            return
        // Submitting hides autocomplete and keeps it hidden until the user types again — kill any
        // pending debounce and block a still-in-flight suggest reply from repopulating the list.
        suggestTimer.stop()
        page.suggestBlocked = true
        page.suggestions = []
        page.lastQuery = q
        page.statusText = ""
        page.statusError = false
        page.hasMore = false
        page.nextStart = 1
        page.loadResults(1)
    }

    // Fetch one page. start<=1 clears + shows the big spinner; later pages append and show the
    // footer spinner. Callback-scoped + query-guarded so a stale page can't clobber the list.
    function loadResults(start) {
        if (start > 1 && page.loadingMore)   // don't stack page-fetches; a fresh search always runs
            return
        var q = page.lastQuery, kind = page.searchKind
        if (start > 1) page.loadingMore = true
        else page.loading = true
        app.backend.searchPage(q, kind, start, (kind === "video" ? page.filterObj() : null), function(res) {
            // Ignore a reply for a page that's since been destroyed (navigated away before it
            // landed → `page` is null), or for a query/kind the user has since changed.
            if (!page || q !== page.lastQuery || kind !== page.searchKind)
                return
            page.loading = false
            page.loadingMore = false
            if (!res || !res.ok) {
                if (start <= 1) {
                    page.statusText = (res && res.error) ? res.error : "search failed"
                    page.statusError = true
                }
                return
            }
            if (start <= 1) {
                resultsModel.clear()
                page.seenIds = ({})     // reset the dedup set for a fresh query
            }
            // ytsearch pagination can hand back overlapping windows; dedup by id/url so the same
            // video never appears twice. If a "load more" page adds nothing new, we've hit the
            // end of usefully-distinct results — stop paging rather than repeat forever.
            var items = res.items || []
            var added = 0
            for (var i = 0; i < items.length; i++) {
                var it = items[i]
                var key = it.id || it.url || ""
                if (key.length > 0 && page.seenIds[key])
                    continue
                if (key.length > 0)
                    page.seenIds[key] = true
                resultsModel.append(it)
                added++
            }
            page.hasMore = !!res.has_more && (start <= 1 || added > 0)
            page.nextStart = start + page.pageSize
        })
    }

    function fmtDur(sec) {
        if (!sec || sec <= 0) return ""
        sec = Math.floor(sec)
        var h = Math.floor(sec / 3600)
        var m = Math.floor((sec % 3600) / 60)
        var s = sec % 60
        function p(nn) { return (nn < 10 ? "0" : "") + nn }
        return (h > 0 ? h + ":" + p(m) : "" + m) + ":" + p(s)
    }
    function fmtCount(n) {
        if (!n || n < 0) return "0"
        if (n >= 1000000) return (n / 1000000).toFixed(1).replace(".0", "") + "M"
        if (n >= 1000) return (n / 1000).toFixed(1).replace(".0", "") + "K"
        return "" + n
    }

    ListModel { id: resultsModel }

    // Debounced autocomplete: refetch ~300ms after the last keystroke, ignore stale replies.
    Timer {
        id: suggestTimer
        interval: 300
        onTriggered: {
            if (page.suggestBlocked) return
            var q = page.queryText
            if (q.length === 0) { page.suggestions = []; return }
            app.backend.suggest(q, function(list) {
                if (page && page.queryText === q && !page.suggestBlocked) page.suggestions = list
            })
        }
    }

    // Search controls live OUTSIDE the ListView. As a list header the SearchField lost active focus
    // every time results arrived (the view re-lays-out its header), which dropped the keyboard;
    // keeping it out lets it hold focus and auto-raise the keyboard on open (matches FinTune).
    Column {
        id: topArea
        anchors { top: parent.top; left: parent.left; right: parent.right }
        z: 2

        PageHeader { title: "Search" }

        Row {
            width: parent.width
            SearchField {
                id: searchField
                // Trim an extra page margin so the cog sits inset from the right edge, matching the
                // magnifying glass's gap on the left (the cog is the Row's last item → the freed
                // width becomes a right-edge gap).
                width: parent.width - (filterCog.visible ? filterCog.width + Theme.horizontalPageMargin : 0)
                placeholderText: page.searchKind === "channel" ? "Search channels" : "Search YouTube"
                EnterKey.iconSource: "image://theme/icon-m-enter-accept"
                EnterKey.onClicked: page.runSearch(text)
                onTextChanged: {
                    page.queryText = text
                    page.suggestBlocked = false   // user is typing again → allow suggestions
                    if (text.length === 0) { page.suggestions = []; suggestTimer.stop() }
                    else suggestTimer.restart()
                }
                Component.onCompleted: forceActiveFocus()   // raise the keyboard when the page opens
            }
            // Cog → toggle the filter panel (video search only; tinted when a filter is active).
            IconButton {
                id: filterCog
                anchors.verticalCenter: searchField.verticalCenter
                visible: page.searchKind === "video"
                icon.source: "image://theme/icon-m-setting"
                             + (page.filtersActive ? "?" + Theme.highlightColor : "")
                onClicked: page.filtersOpen = !page.filtersOpen
            }
        }

        // Filter panel — toggled by the cog. Each ComboBox re-runs the query on change; the indices
        // map to the `sp` codes consumed by filterObj() → youfish._search_filter_sp().
        Column {
            width: parent.width
            visible: page.filtersOpen && page.searchKind === "video"

            ComboBox {
                width: parent.width
                label: "Sort by"
                currentIndex: 0
                menu: ContextMenu {
                    MenuItem { text: "Relevance" }
                    MenuItem { text: "Upload date" }
                    MenuItem { text: "View count" }
                    MenuItem { text: "Rating" }
                }
                onCurrentIndexChanged: { page.fSort = [0, 2, 3, 1][currentIndex]; page.applyFilters() }
            }
            ComboBox {
                width: parent.width
                label: "Uploaded"
                currentIndex: 0
                menu: ContextMenu {
                    MenuItem { text: "Any time" }
                    MenuItem { text: "Today" }
                    MenuItem { text: "This week" }
                    MenuItem { text: "This month" }
                    MenuItem { text: "This year" }
                }
                onCurrentIndexChanged: { page.fDate = [0, 2, 3, 4, 5][currentIndex]; page.applyFilters() }
            }
            ComboBox {
                width: parent.width
                label: "Length"
                currentIndex: 0
                menu: ContextMenu {
                    MenuItem { text: "Any" }
                    MenuItem { text: "Under 4 minutes" }
                    MenuItem { text: "4 – 20 minutes" }
                    MenuItem { text: "Over 20 minutes" }
                }
                onCurrentIndexChanged: { page.fDur = [0, 1, 3, 2][currentIndex]; page.applyFilters() }
            }
        }
    }

    SilicaListView {
        id: listView
        anchors { top: topArea.bottom; bottom: parent.bottom
                  left: parent.left; right: parent.right }
        model: resultsModel

        // Page in more results when scrolled to the end (mirrors ChannelPage).
        onAtYEndChanged: {
            if (atYEnd && page.hasMore && !page.loadingMore && !page.loading
                    && resultsModel.count > 0)
                page.loadResults(page.nextStart)
        }

        footer: Item {
            width: listView.width
            height: (page.loadingMore || (page.hasMore && resultsModel.count > 0))
                    ? Theme.itemSizeLarge : 0
            BusyIndicator {
                anchors.centerIn: parent
                size: BusyIndicatorSize.Medium
                running: page.loadingMore
            }
        }

        header: Column {
            width: listView.width

            // Autocomplete suggestions (tap to search that term).
            Column {
                width: parent.width
                visible: page.suggestions.length > 0
                Repeater {
                    model: page.suggestions
                    BackgroundItem {
                        width: parent.width
                        height: Theme.itemSizeSmall
                        Image {
                            id: sIcon
                            anchors {
                                left: parent.left; leftMargin: Theme.horizontalPageMargin
                                verticalCenter: parent.verticalCenter
                            }
                            source: "image://theme/icon-m-search"
                            width: Theme.iconSizeSmall; height: width
                            opacity: 0.5
                        }
                        Label {
                            anchors {
                                left: sIcon.right; leftMargin: Theme.paddingMedium
                                right: parent.right; rightMargin: Theme.horizontalPageMargin
                                verticalCenter: parent.verticalCenter
                            }
                            text: modelData
                            truncationMode: TruncationMode.Fade
                            color: highlighted ? Theme.highlightColor : Theme.primaryColor
                        }
                        onClicked: {
                            searchField.text = modelData
                            page.runSearch(modelData)
                        }
                    }
                }
            }

            ComboBox {
                id: kindCombo
                width: parent.width
                label: "Search for"
                currentIndex: 0
                menu: ContextMenu {
                    MenuItem { text: "Videos" }
                    MenuItem { text: "Channels" }
                }
                // Switching kind re-runs the current query so results update immediately.
                onCurrentIndexChanged: {
                    page.searchKind = currentIndex === 1 ? "channel" : "video"
                    if (page.lastQuery.length > 0) {
                        page.statusText = ""
                        page.hasMore = false
                        page.nextStart = 1
                        page.loadResults(1)
                    }
                }
            }

            Label {
                visible: page.statusText.length > 0
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                wrapMode: Text.Wrap
                color: page.statusError ? Theme.errorColor : Theme.secondaryHighlightColor
                font.pixelSize: Theme.fontSizeSmall
                text: page.statusText
            }
        }

        delegate: ListItem {
            id: item
            width: listView.width
            property bool isChannel: model.type === "channel"
            property string vidId: model.id
            property string vidTitle: model.title
            property string vidUploader: model.uploader || ""
            property real vidDuration: model.duration || 0
            property string vidThumb: model.thumbnail || ""
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: isChannel ? Theme.itemSizeLarge : (thumbH + 2 * Theme.paddingSmall)

            // Long-press → download (video results only; a channel has nothing to download).
            menu: item.isChannel ? null : dlMenu
            Component {
                id: dlMenu
                ContextMenu {
                    MenuItem {
                        text: (app.backend.watchMap && app.backend.watchMap[item.vidId]
                               && app.backend.watchMap[item.vidId].w) ? "Mark as unwatched" : "Mark as watched"
                        onClicked: {
                            var e = app.backend.watchMap ? app.backend.watchMap[item.vidId] : undefined
                            app.backend.setWatched(item.vidId, !(e && e.w), item.vidTitle, item.vidUploader)
                        }
                    }
                    MenuItem {
                        text: "Video details"
                        onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                            { videoId: item.vidId, title: item.vidTitle, infoOnly: true,
                              infoThumbnail: item.vidThumb })
                    }
                    MenuItem {
                        text: "Add to playlist"
                        onClicked: pageStack.push(Qt.resolvedUrl("AddToPlaylistPage.qml"),
                            { video: { id: item.vidId, title: item.vidTitle, uploader: item.vidUploader,
                                       duration: item.vidDuration, thumbnail: item.vidThumb } })
                    }
                    MenuItem {
                        text: "Download audio"
                        onClicked: Remorse.popupAction(page, "Downloading audio",
                            function() { app.backend.download(item.vidId, item.vidTitle, "audio") })
                    }
                    MenuItem {
                        text: "Download video"
                        onClicked: Remorse.popupAction(page, "Downloading video",
                            function() { app.backend.download(item.vidId, item.vidTitle, "video") })
                    }
                }
            }

            // ---- Video result: 16:9 thumbnail (with duration badge) + title/uploader ----
            Item {
                anchors.fill: parent
                visible: !item.isChannel

                Rectangle {
                    id: vThumb
                    anchors {
                        left: parent.left; leftMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    width: item.thumbW; height: item.thumbH
                    radius: Theme.paddingSmall / 2
                    color: Theme.rgba(Theme.secondaryColor, 0.2)
                    clip: true

                    Image {
                        anchors.fill: parent
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        source: item.isChannel ? "" : (model.thumbnail || "")
                    }
                    WatchOverlay {
                        anchors.fill: parent
                        videoId: item.isChannel ? "" : (model.id || "")
                        live: !item.isChannel && !!model.live
                    }
                    Rectangle {
                        visible: model.duration > 0
                        anchors {
                            right: parent.right; bottom: parent.bottom
                            margins: Theme.paddingSmall / 2
                        }
                        radius: 3
                        color: "#C8000000"
                        width: durLabel.width + Theme.paddingSmall
                        height: durLabel.height + Theme.paddingSmall / 2
                        Label {
                            id: durLabel
                            anchors.centerIn: parent
                            text: page.fmtDur(model.duration)
                            color: "white"
                            font.pixelSize: Theme.fontSizeExtraSmall
                        }
                    }
                }
                Column {
                    anchors {
                        left: vThumb.right; leftMargin: Theme.paddingMedium
                        right: parent.right; rightMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    Label {
                        width: parent.width
                        text: model.title
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                        color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                        font.pixelSize: Theme.fontSizeSmall
                    }
                    Label {
                        width: parent.width
                        visible: text.length > 0
                        text: model.uploader
                        truncationMode: TruncationMode.Fade
                        color: Theme.secondaryColor
                        font.pixelSize: Theme.fontSizeExtraSmall
                    }
                    Label {
                        width: parent.width
                        visible: text.length > 0
                        // views · age on their own line (channel name is above)
                        text: {
                            var parts = []
                            if (model.views > 0) parts.push(page.fmtCount(model.views) + " views")
                            if (model.posted) parts.push(model.posted)
                            return parts.join("  ·  ")
                        }
                        truncationMode: TruncationMode.Fade
                        color: Theme.secondaryColor
                        font.pixelSize: Theme.fontSizeExtraSmall
                    }
                }
            }

            // ---- Channel result: avatar + name (+ subscriber count) ----
            Item {
                anchors.fill: parent
                visible: item.isChannel

                Image {
                    id: cAvatar
                    anchors {
                        left: parent.left; leftMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    width: Theme.itemSizeMedium; height: width
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    smooth: true
                    source: item.isChannel ? (model.thumbnail || "") : ""
                }
                Column {
                    anchors {
                        left: cAvatar.right; leftMargin: Theme.paddingMedium
                        right: parent.right; rightMargin: Theme.horizontalPageMargin
                        verticalCenter: parent.verticalCenter
                    }
                    Label {
                        width: parent.width
                        text: model.title
                        truncationMode: TruncationMode.Fade
                        color: item.highlighted ? Theme.highlightColor : Theme.primaryColor
                    }
                    Label {
                        width: parent.width
                        visible: model.subscribers > 0
                        text: page.fmtCount(model.subscribers) + " subscribers"
                        color: Theme.secondaryColor
                        font.pixelSize: Theme.fontSizeExtraSmall
                    }
                }
            }

            onClicked: {
                if (item.isChannel)
                    pageStack.push(Qt.resolvedUrl("ChannelPage.qml"),
                        { channelRef: model.url || model.id,
                          channelName: model.title,
                          channelId: model.id,
                          channelThumb: model.thumbnail || "" })
                else
                    pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                        { videoId: model.id, title: model.title })
            }
        }

        ViewPlaceholder {
            enabled: resultsModel.count === 0 && !page.loading
            text: !app.backend.ready ? "yt-dlp not found"
                  : (page.searchKind === "channel" ? "Search for a channel" : "Search for a video")
            hintText: app.backend.ready
                      ? "" : "Install yt-dlp on the device, then pull down to recheck"
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading || app.backend.updating
    }
}
