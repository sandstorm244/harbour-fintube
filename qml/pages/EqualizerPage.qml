import QtQuick 2.0
import Sailfish.Silica 1.0

// 10-band equalizer. Bands map to equalizer-10bands' fixed centre frequencies. Adjusting a band
// previews live on the active player; the value is persisted (and re-applied across videos) on
// release. Presets set all ten at once.
Page {
    id: page
    allowedOrientations: Orientation.All

    readonly property var freqLabels: ["29 Hz", "59 Hz", "119 Hz", "237 Hz", "474 Hz",
                                       "947 Hz", "1.9 kHz", "3.8 kHz", "7.5 kHz", "15 kHz"]
    // Preset curves (dB per band). "Flat" (all zero) is index 0.
    readonly property var presetNames: ["Flat", "Bass boost", "Treble boost", "Rock",
                                        "Pop", "Vocal", "Electronic"]
    readonly property var presets: [
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        [6, 5, 4, 2, 0, 0, 0, 0, 0, 0],
        [0, 0, 0, 0, 0, 0, 2, 4, 5, 6],
        [5, 3, -1, -2, -1, 1, 3, 4, 4, 4],
        [-1, 2, 4, 4, 2, 0, -1, -1, 0, 1],
        [-2, -1, 0, 2, 4, 4, 3, 2, 0, -1],
        [4, 3, 0, -2, -3, 0, 2, 3, 4, 5]
    ]

    function commitBand(i, v) {
        var b = app.backend.eqBands.slice()
        b[i] = v
        app.backend.setEqBands(b)
    }
    function applyPreset(idx) {
        if (idx < 0 || idx >= presets.length) return
        app.backend.setEqBands(presets[idx].slice())
        if (idx !== 0 && !app.backend.eqEnabled)
            app.backend.setEqEnabled(true)          // picking a curve implies you want it on
    }

    SilicaFlickable {
        anchors.fill: parent
        contentHeight: col.height + Theme.paddingLarge

        Column {
            id: col
            width: parent.width
            spacing: Theme.paddingMedium

            PageHeader { title: "Equalizer" }

            TextSwitch {
                text: "Equalizer"
                description: "Apply the band gains below to playback."
                automaticCheck: false
                checked: app.backend.eqEnabled
                onClicked: app.backend.setEqEnabled(!app.backend.eqEnabled)
            }

            ComboBox {
                width: parent.width
                label: "Preset"
                // Apply on the item's own click (not currentIndex, which fires on load and would
                // reset the saved bands to Flat every time the page opens).
                menu: ContextMenu {
                    Repeater {
                        model: page.presetNames
                        MenuItem {
                            text: modelData
                            onClicked: page.applyPreset(index)
                        }
                    }
                }
            }

            Item { width: 1; height: Theme.paddingMedium }

            Repeater {
                model: 10
                delegate: Slider {
                    width: col.width
                    // Musical range; the engine clamps to -24..+12 regardless.
                    minimumValue: -12
                    maximumValue: 12
                    stepSize: 1
                    enabled: app.backend.eqEnabled
                    value: app.backend.eqBands[index] || 0
                    label: page.freqLabels[index]
                    valueText: (value > 0 ? "+" : "") + Math.round(value) + " dB"
                    // Live preview while dragging; persist + re-establish the binding on release.
                    onValueChanged: if (app.activePlayer) app.activePlayer.setEqBand(index, value)
                    onReleased: {
                        page.commitBand(index, value)
                        value = Qt.binding(function() { return app.backend.eqBands[index] || 0 })
                    }
                }
            }
        }

        VerticalScrollDecorator { }
    }
}
