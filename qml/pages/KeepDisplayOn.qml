import QtQuick 2.0
import Nemo.KeepAlive 1.2

// Loaded once per player page by KeepAwake.qml, which switches preventBlanking on while a video
// visibly plays. While on, it stops the display's auto-blank timeout (and with it the display-off/on
// GL corruption mid-playback); switching it off hands the screen back to the system's timeout.
DisplayBlanking {
    preventBlanking: false
}
