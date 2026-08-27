import QtQuick 2.0
import Sailfish.Silica 1.0

// "More" — a launcher for the secondary destinations that used to crowd the home pull-down.
// Reached with a forward (right-to-left) swipe from Home; the back swipe returns to Home. The
// home attaches it via pageStack.pushAttached("MorePage.qml", { entries: [...] }).
//
// Each entry: { title, desc (optional subtitle), page (qml file to push), icon (optional) }.
// `icon` accepts a theme name ("icon-m-foo" -> image://theme/…, auto-tinted) OR any image URL
// (e.g. "../icons/library.png") — drop a custom glyph in whenever you make one; leave it out and
// the row is clean type only.
Page {
    id: page
    allowedOrientations: Orientation.All

    property var entries: []
    property string heading: "More"

    SilicaListView {
        anchors.fill: parent
        model: page.entries
        header: PageHeader { title: page.heading }

        delegate: BackgroundItem {
            id: row
            width: ListView.view.width
            height: Theme.itemSizeLarge
            onClicked: pageStack.push(Qt.resolvedUrl(modelData.page))

            Row {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                height: parent.height
                spacing: Theme.paddingLarge

                // Optional glyph. Invisible (and skipped by the Row) until an entry sets `icon`,
                // so a custom icon set can be added later without touching this file.
                Image {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: source != ""
                    width: Theme.iconSizeMedium
                    height: width
                    fillMode: Image.PreserveAspectFit
                    source: modelData.icon
                            ? (modelData.icon.indexOf("icon-") === 0
                               ? "image://theme/" + modelData.icon + (row.highlighted ? "?" + Theme.highlightColor : "")
                               : modelData.icon)
                            : ""
                }

                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width

                    Label {
                        text: modelData.title
                        font.pixelSize: Theme.fontSizeLarge
                        color: row.highlighted ? Theme.highlightColor : Theme.primaryColor
                    }
                    Label {
                        width: parent.width
                        visible: text !== ""
                        text: modelData.desc || ""
                        font.pixelSize: Theme.fontSizeExtraSmall
                        color: row.highlighted ? Theme.secondaryHighlightColor : Theme.secondaryColor
                        truncationMode: TruncationMode.Fade
                    }
                }
            }
        }
    }
}
