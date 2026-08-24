import QtQuick 2.0
import Nemo.KeepAlive 1.2

// Instantiated (via a Loader) only while a video is playing and the "keep display on" setting is
// enabled. Prevents the display's auto-blank timeout so playback isn't interrupted — and, as a
// bonus, sidesteps the display-off/on GL corruption because the screen never blanks mid-playback.
DisplayBlanking {
    preventBlanking: true
}
