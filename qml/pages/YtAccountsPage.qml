import QtQuick 2.0
import Sailfish.Silica 1.0

// YouTube account picker. One imported browser session can hold several signed-in Google logins and,
// within a login, several channels (brand accounts). Pick which one FinTube acts as for importing
// subscriptions/playlists. The list comes from youfish.youtube_list_accounts (the InnerTube account
// switcher); tapping one persists it (X-Goog-AuthUser + X-Goog-PageId) and re-imports use it.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true
    property bool loading: true
    property string errorText: ""

    function reload() {
        page.loading = true
        page.errorText = ""
        app.backend.ytmListAccounts(function(res) {
            page.loading = false
            if (res && !res.ok)
                page.errorText = res.error || "Couldn't load the account list."
        })
    }

    Component.onCompleted: page.reload()

    SilicaListView {
        anchors.fill: parent
        model: app.backend.ytAccounts

        PullDownMenu {
            MenuItem { text: "Reload"; onClicked: page.reload() }
        }

        header: Column {
            width: parent.width
            PageHeader { title: "Account" }
            Label {
                x: Theme.horizontalPageMargin
                width: parent.width - 2 * Theme.horizontalPageMargin
                bottomPadding: Theme.paddingMedium
                wrapMode: Text.Wrap
                text: "Which account/channel to import subscriptions and playlists from. Re-run "
                      + "More → Import from YouTube after switching."
                color: Theme.secondaryColor
                font.pixelSize: Theme.fontSizeExtraSmall
            }
        }

        delegate: ListItem {
            id: item
            width: ListView.view.width
            contentHeight: Theme.itemSizeLarge

            Image {
                id: avatar
                anchors {
                    left: parent.left; leftMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                width: (modelData.thumb && modelData.thumb.length > 0) ? Theme.itemSizeMedium : 0
                height: Theme.itemSizeMedium
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                smooth: true
                sourceSize: Qt.size(Theme.itemSizeMedium, Theme.itemSizeMedium)   // #9: decode to avatar size
                source: modelData.thumb || ""
            }
            Column {
                anchors {
                    left: avatar.right
                    leftMargin: (avatar.width > 0) ? Theme.paddingMedium : Theme.horizontalPageMargin
                    right: parent.right; rightMargin: Theme.horizontalPageMargin
                    verticalCenter: parent.verticalCenter
                }
                Label {
                    width: parent.width
                    text: modelData.name + (modelData.selected ? "   ✓" : "")
                    truncationMode: TruncationMode.Fade
                    color: (item.highlighted || modelData.selected)
                           ? Theme.highlightColor : Theme.primaryColor
                }
                Label {
                    width: parent.width
                    visible: text.length > 0
                    text: modelData.handle || ""
                    truncationMode: TruncationMode.Fade
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                }
            }

            onClicked: app.backend.ytmSelectAccount(modelData, function(res) {
                if (res && res.ok)
                    pageStack.pop()
                else
                    page.errorText = (res && res.error) ? res.error : "Couldn't switch account."
            })
        }

        ViewPlaceholder {
            enabled: !page.loading && app.backend.ytAccounts.length === 0
            text: page.errorText.length > 0 ? page.errorText : "No accounts found"
            hintText: app.backend.youtubeLoggedIn
                      ? "Pull down to reload" : "Import your YouTube login first"
        }

        VerticalScrollDecorator { }
    }

    BusyIndicator {
        anchors.centerIn: parent
        size: BusyIndicatorSize.Large
        running: page.loading
    }
}
