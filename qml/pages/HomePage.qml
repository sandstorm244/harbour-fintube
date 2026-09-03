import QtQuick 2.0
import Sailfish.Silica 1.0

// Home: one feed of the newest uploads across all subscribed channels (built from each
// channel's RSS by the Python backend). Search, channel management and settings hang off
// the pull-down menu.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool loading: true
    property int lastSubsCount: -1

    ListModel { id: feedModel }

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

    function loadFeed(force) {
        if (feedModel.count === 0)
            page.loading = true
        // A user-driven load (pull-to-refresh) forces EVERY channel; the initial load just reads
        // the cache (stale-while-revalidate) and lets the background refresh handle due channels.
        app.backend.subscriptionFeed(force, force, function(res) {
            page.loading = false
            page.lastSubsCount = app.backend.subscriptions.length
            feedModel.clear()
            var items = (res && res.items) ? res.items : []
            for (var i = 0; i < items.length; i++)
                feedModel.append(items[i])
            // The feed is built from each channel's RSS (fast, spawn-free) which carries no video
            // length or Shorts flag — backfill both from yt-dlp in the background so the length
            // badges appear a beat after the feed and any Shorts drop out.
            if (feedModel.count > 0)
                page.fillDurations(force)
            // Stale-while-revalidate: the response may be a cached (even hours-old) feed shown
            // instantly on a cold launch. If it's stale, fetch a fresh one in the background and
            // swap it in silently — so the user never stares at a spinner while a refresh runs.
            if (!force && res && res.cached && res.stale)
                page.refreshFeedInBackground()
        })
    }

    // Background refresh (no spinner). Repopulates only when the feed actually changed up top, so
    // an unchanged feed doesn't reset the user's scroll position out from under them.
    function refreshFeedInBackground() {
        app.backend.subscriptionFeed(true, false, function(res) {   // DUE channels only (adaptive TTL)
            var items = (res && res.items) ? res.items : []
            if (items.length === 0)
                return
            // Repopulate only if the id SEQUENCE actually changed — catches any new / removed /
            // reordered video anywhere in the list (not just a new top one), while still skipping a
            // true no-op so an unchanged feed doesn't reset the user's scroll.
            var changed = items.length !== feedModel.count
            for (var j = 0; !changed && j < items.length; j++)
                if (feedModel.get(j).id !== items[j].id)
                    changed = true
            if (!changed)
                return
            feedModel.clear()
            for (var i = 0; i < items.length; i++)
                feedModel.append(items[i])
            page.fillDurations(false)   // fresh RSS rows carry no length/Shorts flag → backfill again
        })
    }

    // RSS has no video length or Shorts flag; pull both from yt-dlp in the background. Drop the
    // durations into the rows so the length badges appear a moment after the feed, and remove any
    // row now known to be a Short (channel /shorts-tab membership — reliable regardless of length).
    function fillDurations(force) {
        app.backend.feedDurations(force, function(map, shorts) {
            var shortSet = Object.create(null)   // no prototype → an id like "constructor" can't false-hit
            if (app.backend.hideShorts && shorts)
                for (var s = 0; s < shorts.length; s++)
                    shortSet[shorts[s]] = true
            for (var i = feedModel.count - 1; i >= 0; i--) {   // backwards → safe removal
                var it = feedModel.get(i)
                if (shortSet[it.id]) {
                    feedModel.remove(i)
                    continue
                }
                var d = map ? map[it.id] : undefined
                if (d !== undefined && d > 0 && it.duration !== d)
                    feedModel.setProperty(i, "duration", d)
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
                heading: "More",
                entries: [
                    { title: "Downloads", desc: "Saved videos for offline", page: "DownloadsPage.qml" },
                    { title: "Playlists", desc: "Your local playlists", page: "PlaylistsPage.qml" },
                    { title: "History", desc: "Recently watched", page: "HistoryPage.qml" },
                    { title: "Channels", desc: "Manage subscriptions", page: "SubscriptionsPage.qml" },
                    { title: "Settings", desc: "Playback + content", page: "SettingsPage.qml" },
                    { title: "Providers", desc: "yt-dlp, ffmpeg, PO-token provider", page: "ProvidersPage.qml" },
                    { title: "Import from NewPipe", desc: "Subs, history & playlists from a backup",
                      action: "import-newpipe" },
                    { title: "Import from YouTube", desc: "Your subscriptions & playlists (needs sign-in)",
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
                      ? ("Installing yt-dlp… " + Math.round(app.backend.installPct) + "%")
                      : "Install yt-dlp"
                enabled: !app.backend.installing
                onClicked: {
                    app.backend.installYtdlp()
                    pageStack.push(Qt.resolvedUrl("SettingsPage.qml"))
                }
            }
            MenuItem {
                text: "Search"
                onClicked: pageStack.push(Qt.resolvedUrl("SearchPage.qml"))
            }
            MenuItem {
                text: "Refresh"
                onClicked: page.loadFeed(true)
            }
        }

        header: PageHeader { title: "Subscriptions" }

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
                           && app.backend.watchMap[model.id].w) ? "Mark as unwatched" : "Mark as watched"
                    onClicked: {
                        var e = app.backend.watchMap ? app.backend.watchMap[model.id] : undefined
                        app.backend.setWatched(model.id, !(e && e.w), model.title, model.uploader || "")
                    }
                }
                MenuItem {
                    text: "Video details"
                    onClicked: pageStack.push(Qt.resolvedUrl("VideoPage.qml"),
                        { videoId: model.id, title: model.title, infoOnly: true,
                          infoThumbnail: model.thumbnail || "" })
                }
                MenuItem {
                    text: "Add to playlist"
                    onClicked: pageStack.push(Qt.resolvedUrl("AddToPlaylistPage.qml"),
                        { video: { id: model.id, title: model.title, uploader: model.uploader || "",
                                   duration: model.duration || 0, thumbnail: model.thumbnail || "" } })
                }
                MenuItem {
                    text: "Download audio"
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, "Downloading audio",
                            function() { app.backend.download(vid, t, "audio") })
                    }
                }
                MenuItem {
                    text: "Download video"
                    onClicked: {
                        var vid = model.id, t = model.title
                        Remorse.popupAction(page, "Downloading video",
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
                        if (model.views > 0) parts.push(page.fmtCount(model.views) + " views")
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
            text: app.backend.subscriptions.length === 0 ? "No subscriptions yet" : "No recent uploads"
            hintText: app.backend.subscriptions.length === 0
                      ? "Pull down → Search to find channels to subscribe to" : ""
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
