#ifndef VIDEOPLAYER_H
#define VIDEOPLAYER_H

#include <QQuickPaintedItem>
#include <QImage>
#include <QMutex>
#include <QString>
#include <QTimer>
#include <QElapsedTimer>
#include <gst/gst.h>

class HwVideoSink;

// A GStreamer video player rendered into the QML scene via QQuickPaintedItem.
// Plays YouTube's separate video-only + audio-only DASH tracks through one
// pipeline (two uridecodebins, shared clock), sets the HTTP User-Agent on the
// source so googlevideo doesn't 403, and forces software decode for v1 so the
// frames are CPU-mappable for the appsink.
class VideoPlayer : public QQuickPaintedItem
{
    Q_OBJECT
    Q_PROPERTY(QString videoUrl READ videoUrl WRITE setVideoUrl NOTIFY videoUrlChanged)
    Q_PROPERTY(QString audioUrl READ audioUrl WRITE setAudioUrl NOTIFY audioUrlChanged)
    Q_PROPERTY(QString userAgent READ userAgent WRITE setUserAgent NOTIFY userAgentChanged)
    Q_PROPERTY(bool playing READ playing NOTIFY playingChanged)
    // Position and duration are in milliseconds, polled off the pipeline clock.
    Q_PROPERTY(qint64 position READ position NOTIFY positionChanged)
    Q_PROPERTY(qint64 duration READ duration NOTIFY durationChanged)
    // Playback speed (1.0 = normal); applied via a rate-seek, pitch-corrected by scaletempo.
    Q_PROPERTY(qreal rate READ rate WRITE setRate NOTIFY rateChanged)
    // Hardware decode: route video through droidvdec->droideglsink instead of the software
    // appsink. Set from Settings before play(); YOUFISH_HWDEC=1 forces it on for testing.
    Q_PROPERTY(bool hwDecode READ hwDecode WRITE setHwDecode NOTIFY hwDecodeChanged)

public:
    explicit VideoPlayer(QQuickItem *parent = nullptr);
    ~VideoPlayer() override;

    void paint(QPainter *painter) override;

    QString videoUrl() const { return m_videoUrl; }
    QString audioUrl() const { return m_audioUrl; }
    QString userAgent() const { return m_userAgent; }
    bool playing() const { return m_playing; }
    qint64 position() const { return m_position; }
    qint64 duration() const { return m_duration; }
    qreal rate() const { return m_rate; }
    bool hwDecode() const { return m_hwDecodeReq; }

    void setVideoUrl(const QString &url);
    void setAudioUrl(const QString &url);
    void setUserAgent(const QString &ua);
    void setRate(qreal rate);
    void setHwDecode(bool on);

    Q_INVOKABLE void play();
    Q_INVOKABLE void pause();
    Q_INVOKABLE void stop();
    Q_INVOKABLE void seek(qint64 positionMs);
    // Freeze/thaw just the video branch (audio keeps playing) when the app is hidden, so we
    // don't decode frames nobody can see. Auto-driven from QML by the app's active state.
    Q_INVOKABLE void setVideoActive(bool active);

    // Force a repaint so the current frame is re-uploaded from CPU memory. Called on resume from
    // a display-off/on cycle, which can drop/corrupt the frame's GL texture (black frame on wake).
    Q_INVOKABLE void requestRepaint();

    // 10-band equalizer (equalizer-10bands, spliced after scaletempo). Values persist across
    // pipeline rebuilds, so QML sets them once from settings and on each change. Gains are dB
    // (clamped -24..+12); disabled = flat (transparent). No-op if the plugin is unavailable.
    Q_INVOKABLE void setEqEnabled(bool on);
    Q_INVOKABLE void setEqBand(int index, double gainDb);

    // Volume boost above the system maximum, with a soft limiter after it so the extra gain
    // doesn't hard-clip (the distortion raw >100% causes). gain is linear (1.0 = no boost).
    Q_INVOKABLE void setBoost(double gain);

signals:
    void videoUrlChanged();
    void audioUrlChanged();
    void userAgentChanged();
    void playingChanged();
    void positionChanged();
    void durationChanged();
    void rateChanged();
    void hwDecodeChanged();
    void errorOccurred(const QString &message);
    void ended();                     // video reached its natural end (for autoplay queues)

private:
    void buildPipeline();
    void teardown();
    void setError(const QString &message);
    void updatePosition();
    void sendSeek(qint64 positionMs);
    void applyEqBands();          // push m_eqBands (or flat, when disabled) onto the live element

    static void onSourceSetup(GstElement *bin, GstElement *source, gpointer self);
    static void onVideoPad(GstElement *bin, GstPad *pad, gpointer self);
    static void onAudioPad(GstElement *bin, GstPad *pad, gpointer self);
    // Muxed streams (one URL carrying both tracks, e.g. YouTube's progressive itag 18 —
    // the only format left when YouTube forces SABR on the adaptive ones): a single
    // uridecodebin whose video AND audio pads are both routed to their sinks.
    static void onMuxedPad(GstElement *bin, GstPad *pad, gpointer self);
    static GstFlowReturn onNewSample(GstElement *sink, gpointer self);
    static gboolean onBusMessage(GstBus *bus, GstMessage *msg, gpointer self);

    QString m_videoUrl;
    QString m_audioUrl;
    QString m_userAgent;
    bool m_playing = false;
    bool m_ended = false;
    qint64 m_position = 0;
    qint64 m_duration = 0;
    qreal m_rate = 1.0;
    bool m_rateEngaged = false;   // non-default start speed engaged once, during preroll
    bool m_videoActive = true;
    bool m_muxed = false;   // single-source mode: m_videoBin carries both video + audio
    bool m_hwDecode = false;// effective mode this build (m_hwDecodeReq, reset each buildPipeline)
    bool m_hwDecodeReq = false; // requested via the hwDecode property / YOUFISH_HWDEC override
    QTimer *m_posTimer = nullptr;
    QElapsedTimer m_prerollTimer;   // play()->first PLAYING, for start-latency profiling
    bool m_prerollLogged = false;

    GstElement *m_pipeline = nullptr;
    GstElement *m_videoBin = nullptr;
    GstElement *m_audioBin = nullptr;
    GstElement *m_videoConvert = nullptr;
    GstElement *m_appsink = nullptr;
    GstElement *m_scaletempo = nullptr;
    GstElement *m_pulsesink = nullptr;
    // Optional 10-band equalizer inserted scaletempo -> eqConvIn -> equalizer -> eqConvOut -> sink.
    GstElement *m_equalizer = nullptr;
    GstElement *m_eqConvIn = nullptr;
    GstElement *m_eqConvOut = nullptr;
    bool m_eqEnabled = false;
    double m_eqBands[10] = {0};
    // Optional volume boost + soft limiter (audioconvert -> volume -> rglimiter) before the sink.
    GstElement *m_dynConv = nullptr;
    GstElement *m_boost = nullptr;
    GstElement *m_limiter = nullptr;
    double m_boostGain = 1.0;
    // Mode-independent handles onto the video branch: m_videoInput is what the decoded video
    // pad links into (videoconvert in SW, droideglsink in HW); m_videoSink is the branch tail
    // we address for seeks (appsink in SW, droideglsink in HW).
    GstElement *m_videoInput = nullptr;
    GstElement *m_videoSink = nullptr;
    GstElement *m_hwDec = nullptr; // explicit droidvdec (hw mode); encoded video links into it
    HwVideoSink *m_hw = nullptr;   // non-null only while the hardware path is active
    // Serialises the render thread's use of m_hw in paint() against teardown()'s delete of it on
    // the GUI thread, so a stop/close landing mid-paint can't free the sink under the renderer.
    QMutex m_hwMutex;
    guint m_busWatch = 0;

    QImage m_frame;
    QMutex m_frameMutex;
};

#endif // VIDEOPLAYER_H
