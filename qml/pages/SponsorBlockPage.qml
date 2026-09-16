import QtQuick 2.0
import Sailfish.Silica 1.0

// SponsorBlock configuration — split off Settings so the main page isn't cluttered. A master switch
// plus a per-category action: Off (ignore), Auto-skip (jump past it silently), or Show skip button
// (a tap-to-skip button appears over the video while the segment plays). The action map lives in
// Backend.sbActions and is persisted as the `sponsorblock_actions` setting; VideoPage reads it live.
Page {
    id: page
    allowedOrientations: Orientation.All

    property bool hideDock: true   // hide the now-playing dock over this settings sub-page

    // Category action <-> ComboBox index. "off" is the absent state (index 0).
    function actionToIndex(a) { return a === "skip" ? 1 : (a === "manual" ? 2 : 0) }
    function indexToAction(i) { return i === 1 ? "skip" : (i === 2 ? "manual" : "off") }

    // The skippable categories SponsorBlock exposes (matches _SB_CATEGORIES in youfish.py), in the
    // order shown. name = the row label, desc = what the segment type covers.
    property var categories: [
        { key: "sponsor",        name: "Sponsor",
          desc: "Paid promotions, paid referrals, and direct advertisements." },
        { key: "selfpromo",      name: "Self-promotion",
          desc: "Unpaid/self promotion: merch, donations, or plugging other creators." },
        { key: "interaction",    name: "Interaction reminder",
          desc: "Reminders to like, subscribe, or follow." },
        { key: "intro",          name: "Intro / intermission",
          desc: "Intro animations or pauses with no real content." },
        { key: "outro",          name: "Endcards / credits",
          desc: "Endcards and credits at the end of the video." },
        { key: "preview",        name: "Preview / recap",
          desc: "A recap of earlier episodes or a preview of upcoming content." },
        { key: "filler",         name: "Filler tangent",
          desc: "Tangential scenes not needed to follow the video." },
        { key: "music_offtopic", name: "Non-music section",
          desc: "Non-music portions of a music video." }
    ]

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "SponsorBlock" }

            TextSwitch {
                text: "Enable SponsorBlock"
                description: "Skip community-marked segments. Fetching them sends the video ID to "
                             + "sponsor.ajay.app; nothing else is shared."
                automaticCheck: false
                checked: app.backend.sponsorBlock
                onClicked: app.backend.setSponsorBlock(!app.backend.sponsorBlock)
            }

            // The per-category controls stay hidden until SponsorBlock is enabled — turning the
            // master switch on "uncovers" them.
            Column {
                width: parent.width
                spacing: Theme.paddingMedium
                visible: app.backend.sponsorBlock

                Label {
                    x: Theme.horizontalPageMargin
                    width: parent.width - 2 * Theme.horizontalPageMargin
                    wrapMode: Text.Wrap
                    color: Theme.secondaryColor
                    font.pixelSize: Theme.fontSizeExtraSmall
                    text: "Auto-skip jumps past a segment silently. Show skip button leaves a "
                          + "tap-to-skip button over the video while the segment plays, so you decide."
                }

                SectionHeader { text: "Categories" }

                Repeater {
                    model: page.categories
                    delegate: ComboBox {
                        id: catCombo
                        property string cat: modelData.key
                        property bool ready: false   // suppress the write fired by the initial binding
                        width: parent.width
                        label: modelData.name
                        description: modelData.desc
                        currentIndex: page.actionToIndex(
                            app.backend.sbActions ? app.backend.sbActions[cat] : "off")
                        menu: ContextMenu {
                            MenuItem { text: "Off" }
                            MenuItem { text: "Auto-skip" }
                            MenuItem { text: "Show skip button" }
                        }
                        onCurrentIndexChanged: {
                            if (!catCombo.ready) return
                            app.backend.setSponsorAction(cat, page.indexToAction(currentIndex))
                        }
                        Component.onCompleted: catCombo.ready = true
                    }
                }
            }
        }
    }
}
