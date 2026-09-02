import QtQuick 2.0
import Sailfish.Silica 1.0

// The saved channels. The model is the backend's live subscriptions array, so
// (un)subscribing anywhere updates this list automatically.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock / resume bar over the Channels list

    SilicaListView {
        id: listView
        anchors.fill: parent
        model: app.backend.subscriptions

        header: PageHeader { title: "Subscriptions" }

        delegate: ListItem {
            id: delegateItem
            width: listView.width

            menu: ContextMenu {
                MenuItem {
                    text: "Unsubscribe"
                    onClicked: app.backend.toggleSubscription(
                        modelData.id, modelData.name, modelData.url, modelData.thumbnail || "")
                }
            }

            Label {
                anchors {
                    left: parent.left; right: parent.right
                    verticalCenter: parent.verticalCenter
                    leftMargin: Theme.horizontalPageMargin
                    rightMargin: Theme.horizontalPageMargin
                }
                text: modelData.name
                truncationMode: TruncationMode.Fade
                color: delegateItem.highlighted ? Theme.highlightColor : Theme.primaryColor
            }

            onClicked: pageStack.push(Qt.resolvedUrl("ChannelPage.qml"),
                { channelRef: modelData.url || modelData.id,
                  channelName: modelData.name,
                  channelId: modelData.id,
                  channelThumb: modelData.thumbnail || "" })
        }

        ViewPlaceholder {
            enabled: app.backend.subscriptions.length === 0
            text: "No subscriptions"
            hintText: "Open a video and tap Subscribe"
        }

        VerticalScrollDecorator { }
    }
}
