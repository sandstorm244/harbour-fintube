import QtQuick 2.0
import Sailfish.Silica 1.0

Page {
    id: page
    allowedOrientations: Orientation.All

    property string parsedKind: ""    // "" | "video" | "channel" | "playlist"
    property string statusText: ""
    property bool statusError: false

    // Strip tracking source identifier
    function stripTracking(t) {
        var q = t.indexOf("?")
        if (q < 0)
            return t
        var parts = t.slice(q + 1).split("&")
        var kept = []
        for (var i = 0; i < parts.length; i++) {
            var key = parts[i].split("=")[0].toLowerCase()
            if (parts[i] && key !== "si" && key !== "is")
                kept.push(parts[i])
        }
        return t.slice(0, q) + (kept.length > 0 ? "?" + kept.join("&") : "")
    }

    function classify(t) {
        page.parsedKind = ""
        page.statusText = ""
        page.statusError = false
        if (!/(^|\.|\/)youtube\.com|(^|\.|\/)youtu\.be/i.test(t))
            return
        var u = page.stripTracking(t)
        page.statusText = qsTr("Checking link…")
        app.backend.parseUrl(u, function(res) {
            if (page.stripTracking(urlField.text.trim()) !== u)
                return    // stale — the text moved on since
            page.parsedKind = (res && res.kind) ? res.kind : ""
            if (page.parsedKind === "video")
                page.statusText = qsTr("Opens a video.")
            else if (page.parsedKind === "channel")
                page.statusText = qsTr("Opens a channel.")
            else if (page.parsedKind === "playlist")
                page.statusText = qsTr("Opens a playlist.")
            else {
                page.statusText = qsTr("That link doesn't point to a video, channel or playlist.")
                page.statusError = true
            }
        })
    }

    function doGo() {
        var t = urlField.text.trim()
        if (t.length === 0)
            return
        app.routeUrl(page.stripTracking(t), function(res) {
            if (!res || !res.kind)
                app.showToast(qsTr("Couldn't open this link"))
        })
    }

    Column {
        id: topArea
        anchors { top: parent.top; left: parent.left; right: parent.right }
        z: 2

        PageHeader { title: qsTr("Go to URL") }

        SearchField {
            id: urlField
            width: parent.width
            placeholderText: "youtube.com/watch?v=…  ·  youtu.be/…"
            EnterKey.iconSource: "image://theme/icon-m-enter-accept"
            EnterKey.onClicked: page.doGo()
            onTextChanged: page.classify(text.trim())
            Component.onCompleted: forceActiveFocus()   // raise the keyboard when the page opens
        }

        Label {
            visible: page.statusText.length > 0
            x: Theme.horizontalPageMargin
            width: parent.width - 2 * Theme.horizontalPageMargin
            wrapMode: Text.Wrap
            text: page.statusText
            color: page.statusError ? Theme.errorColor : Theme.secondaryColor
            font.pixelSize: Theme.fontSizeSmall
        }
    }
}
