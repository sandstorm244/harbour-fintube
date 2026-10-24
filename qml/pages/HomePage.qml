import QtQuick 2.0
import Sailfish.Silica 1.0
import "../logic.js" as Logic

// Home: one feed of the newest uploads across all subscribed channels (built from each
// channel's RSS by the Python backend). Search, channel management and settings hang off
// the pull-down menu.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: true
    // A network revalidation is in flight (explicit Refresh OR a stale-while-revalidate background
    // refresh). Drives the pulsing pulley indicator so the user can see the feed is updating even
    // when the list is already populated (page.loading only covers the empty first-load case).
    property bool refreshing: false
    property int lastSubsCount: -1

    ListModel { id: feedModel }

    function fmtDur(sec) { return Logic.fmtDur(sec) }
    function fmtCount(n) { return Logic.fmtCount(n) }

    function loadFeed(force) {
        if (feedModel.count === 0)
            page.loading = true
        if (force)                       // an explicit Refresh — pulse the pulley while it runs
            page.refreshing = true
        // A user-driven load (pull-to-refresh) forces EVERY channel; the initial load just reads
        // the cache (stale-while-revalidate) and lets the background refresh handle due channels.
        app.backend.subscriptionFeed(force, force, function(res) {
            page.loading = false
            if (force)
                page.refreshing = false
            page.lastSubsCount = app.backend.subscriptions.length
            feedModel.clear()
            var items = (res && res.items) ? res.items : []
            for (var i = 0; i < items.length; i++)
                feedModel.append(items[i])
            // Stale-while-revalidate: the response may be a cached (even hours-old) feed shown
            // instantly on a cold launch. If it's stale, fetch a fresh one in the background and
            // swap it in silently — so the user never stares at a spinner while a refresh runs.
            // The length/Shorts backfill runs AFTER that refresh (both share one lane, in call
            // order) — queued first, it used to hold the refresh behind the whole classification.
            if (!force && res && res.cached && res.stale)
                page.refreshFeedInBackground()
            else if (feedModel.count > 0)
                // RSS carries no video length or Shorts flag — backfill both in the background so
                // the length badges appear a beat after the feed and any Shorts drop out.
                page.fillDurations(force)
        })
    }

    // Background refresh (no spinner). Repopulates only when the feed actually changed up top, so
    // an unchanged feed doesn't reset the user's scroll position out from under them.
    function refreshFeedInBackground() {
        page.refreshing = true
        app.backend.subscriptionFeed(true, false, function(res) {   // DUE channels only (adaptive TTL)
            page.refreshing = false     // clear first — the early returns below must not leave it set
            var items = (res && res.items) ? res.items : []
            if (items.length === 0)
                return
            // Repopulate only if the id SEQUENCE actually changed — catches any new / removed /
            // reordered video anywhere in the list (not just a new top one), while still skipping a
            // true no-op so an unchanged feed doesn't reset the user's scroll.
            var shown = []
            for (var j = 0; j < feedModel.count; j++)
                shown.push(feedModel.get(j).id)
            if (Logic.feedChanged(shown, items)) {
                feedModel.clear()
                for (var i = 0; i < items.length; i++)
                    feedModel.append(items[i])
            }
            page.fillDurations(false)   // RSS rows carry no length/Shorts flag → backfill (cheap no-op
                                        // when everything is already classified)
        })
    }

    // RSS has no video length or Shorts flag; pull both from yt-dlp in the background. Drop the
    // durations into the rows so the length badges appear a moment after the feed, and remove any
    // row now known to be a Short (channel /shorts-tab membership — reliable regardless of length).
    function fillDurations(force) {
        app.backend.feedDurations(force, function(map, shorts) {
            var rows = []
            for (var r = 0; r < feedModel.count; r++) {
                var it = feedModel.get(r)
                rows.push({ id: it.id, duration: it.duration })
            }
            // Edits come in descending index order, so a removal never shifts a later one.
            var edits = Logic.feedBackfill(rows, map, shorts, app.backend.hideShorts)
            for (var i = 0; i < edits.length; i++) {
                if (edits[i].remove)
                    feedModel.remove(edits[i].index)
                else
                    feedModel.setProperty(edits[i].index, "duration", edits[i].duration)
            }
        })
    }

    Component.onCompleted: loadFeed(false)

    // Attach the "More" launcher as a forward (right-to-left swipe) sibling of Home, so the
    // secondary destinations no longer crowd the pull-down. The back swipe returns here.
    property bool _moreAttached: false
    onStatusChanged: {
        if (status === PageStatus.Active && !_moreAttached) {
            _moreAttached = true
            pageStack.pushAttached(Qt.resolvedUrl("MorePage.qml"), {
                heading: qsTr("More"),
                entries: [
                    { title: qsTr("Downloads"), desc: qsTr("Saved videos for offline"), page: "DownloadsPage.qml" },
                    { title: qsTr("Playlists"), desc: qsTr("Your local playlists"), page: "PlaylistsPage.qml" },
                    { title: qsTr("History"), desc: qsTr("Recently watched"), page: "HistoryPage.qml" },
                    { title: qsTr("Channels"), desc: qsTr("Manage subscriptions"), page: "SubscriptionsPage.qml" },
                    { title: qsTr("Settings"), desc: qsTr("Playback + content"), page: "SettingsPage.qml" },
                    { title: qsTr("Providers"), desc: qsTr("yt-dlp, ffmpeg, PO-token provider"), page: "ProvidersPage.qml" },
                    { title: qsTr("SponsorBlock"), desc: qsTr("Skip sponsors and other segments"), page: "SponsorBlockPage.qml" },
                    { title: qsTr("Import from NewPipe"), desc: qsTr("Subs, history & playlists from a backup"),
                      action: "import-newpipe" },
                    { title: qsTr("Import from YouTube"), desc: qsTr("Your subscriptions & playlists (needs sign-in)"),
                      action: "youtube-import" }
                ]
            })
        }
    }

    // Rebuild when the subscription set actually changes (subscribe/unsubscribe, or the
    // list finishing its initial load), not on every return to the page.
    Connections {
        target: app.backend
        onSubscriptionsChanged:
            if (app.backend.subscriptions.length !== page.lastSubsCount)
                page.loadFeed(false)
    }

    // Settings → hide Shorts / hide watched: the engine filters on read, so re-reading the cached
    // feed (no forced network refresh) applies the toggle. Skipped while the first load is in flight
    // (the settings load at startup also flips these) so it can't double-fire the launch refresh.
    Connections {
        target: app.backend
        onHideShortsChanged: if (!page.loading) page.loadFeed(false)
        onHideWatchedChanged: if (!page.loading) page.loadFeed(false)
    }

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: feedModel

        PullDownMenu {
            // Only shown while yt-dlp is missing — without it the app can't resolve or
            // download anything, so make installing it a one-tap action right here.
            MenuItem {
                visible: !app.backend.ready
                text: app.backend.installing
                      ? qsTr("Installing yt-dlp… %1%").arg(Math.round(app.backend.installPct))
                      : qsTr("Install yt-dlp")
                enabled: !app.backend.installing
                onClicked: {
                    app.backend.installYtdlp()
                    pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
                }
            }
            MenuItem {
                text: qsTr("Search")
                onClicked: pageStack.push(Qt.resolvedUrl("SearchPage.qml"))
            }
            MenuItem {
                text: qsTr("Go to URL")
                onClicked: pageStack.push(Qt.resolvedUrl("UrlPage.qml"))
            }
            MenuItem {
                text: page.refreshing ? qsTr("Refreshing…") : qsTr("Refresh")
                enabled: !page.refreshing        // don't stack a second forced walk over a live one
                onClicked: page.loadFeed(true)
            }
        }

        // A small spinner on the left of the title row makes an in-progress feed refresh obvious
        // (the title is right-aligned, so the left side is free). Driven by page.refreshing, same as
        // the Refresh menu item.
        header: Item {
            width: listView.width
            height: subsHeader.height
            PageHeader { id: subsHeader; title: qsTr("Subscriptions") }
            BusyIndicator {
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    bottom: subsHeader.bottom
                    bottomMargin: (Theme.itemSizeLarge - height) / 2   // centre in the title band
                }
                size: BusyIndicatorSize.Small
                running: page.refreshing
                visible: running
            }
        }

        delegate: ListItem {
            id: item
            width: listView.width
            property real thumbW: Math.round(width * 0.42)
            property real thumbH: Math.round(thumbW * 9 / 16)
            contentHeight: thumbH + 2 * Theme.paddingSmall

            // Long-press → mark watched, add to a playlist, or download this video (audio or muxed).
            menu: ContextMenu {
                MenuItem {
                    text: (app.backend.watchMap && app.backend.watchMap[model.id]
                           && app.backend.watchMap[model.id].w) ? qsTr("Mark as unwatched") : qsTr("Mark as watched")
                    onClicked: {
                        var e = app.backend.watchMap ? app.backend.watchMap[model.id] : undefined
                        app.backend.setWatched(model.id, !(e && e.w), model.title, model.uploader || "")
                    }
                }
                MenuItem {
                    text: qsTr("Video details")
                    onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                        { videoId: model.id, title: model.title, infoOnly: true,
                          infoThumbnail: model.thumbnail || "" })
                }
                MenuItem {
                    text: qsTr("Add to playlist")
                    onClicked: pageStack.push(Qt.resolvedUrl("AddToPlaylistPage.qml"),
                        { video: { id: model.id, title: model.title, uploader: model.uploader || "",
                                   duration: model.duration || 0, thumbnail: model.thumbnail || "" } })
                }
                MenuItem {
                    text: qsTr("Download audio")
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, qsTr("Downloading audio"),
                            function() { app.backend.download(vid, t, "audio") })
                    }
                }
                MenuItem {
                    text: qsTr("Download video")
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, qsTr("Downloading video"),
                            function() { app.backend.download(vid, t, "video") })
                    }
                }
            }

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
                    sourceSize: Qt.size(parent.width, parent.height)   // #9: decode to the display box, not full res
                    source: model.thumbnail || ""
                }
                WatchOverlay { anchors.fill: parent; videoId: model.id || ""; live: !!model.live }
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
                    text: {
                        var parts = []
                        if (model.views > 0) parts.push(qsTr("%1 views").arg(page.fmtCount(model.views)))
                        if (model.posted) parts.push(model.posted)
                        return parts.join("  ·  ")
                    }
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                { videoId: model.id, title: model.title })
        }

        ViewPlaceholder {
            enabled: !page.loading && feedModel.count === 0
            text: app.backend.subscriptions.length === 0 ? qsTr("No subscriptions yet") : qsTr("No recent uploads")
            hintText: app.backend.subscriptions.length === 0
                      ? qsTr("Pull down → Search to find channels to subscribe to") : ""
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
