#include "videoplayer.h"
#include "hwvideosink.h"

#include <QPainter>
#include <QMetaObject>
#include <QMatrix4x4>
#include <QDebug>
#include <gst/app/gstappsink.h>
#include <gst/video/video.h>

// Verbose pipeline tracing is gated behind YOUFISH_DEBUG=1 so a normal run stays quiet.
// Warnings and errors (qWarning) always print. Enable with: YOUFISH_DEBUG=1 harbour-youfish
static const bool kYoufishDebug = qEnvironmentVariableIsSet("YOUFISH_DEBUG");
#define YLOG if (kYoufishDebug) qDebug()

VideoPlayer::VideoPlayer(QQuickItem *parent)
    : QQuickPaintedItem(parent)
{
    gst_init(nullptr, nullptr);  // idempotent
    m_posTimer = new QTimer(this);
    m_posTimer->setInterval(500);  // twice a second is smooth enough for a scrubber
    connect(m_posTimer, &QTimer::timeout, this, &VideoPlayer::updatePosition);

    // Hardware decode is chosen by the hwDecode property (from Settings, set before play()),
    // or forced on for testing via YOUFISH_HWDEC=1. Either way the GL renderer needs an
    // FBO-backed painted item — paint() then runs on the scene-graph render thread with a live
    // context. The env override sets the request flag here; the property does it in setHwDecode.
    if (qEnvironmentVariableIsSet("YOUFISH_HWDEC")) {
        m_hwDecodeReq = true;
        setRenderTarget(QQuickPaintedItem::FramebufferObject);
        setSmooth(false);
        setAntialiasing(false);
    }
}

void VideoPlayer::setHwDecode(bool on)
{
    if (m_hwDecodeReq == on)
        return;
    m_hwDecodeReq = on;
    // Switch to the FBO target as soon as hw is requested so the GL renderer can paint. We only
    // ever switch TO it (the software QImage path renders fine under an FBO target too), so
    // turning the toggle back off mid-session disturbs nothing.
    if (on) {
        setRenderTarget(QQuickPaintedItem::FramebufferObject);
        setSmooth(false);
        setAntialiasing(false);
    }
    emit hwDecodeChanged();
}

VideoPlayer::~VideoPlayer()
{
    teardown();
}

void VideoPlayer::setVideoUrl(const QString &url)
{
    if (m_videoUrl == url)
        return;
    m_videoUrl = url;
    emit videoUrlChanged();
}

void VideoPlayer::setAudioUrl(const QString &url)
{
    if (m_audioUrl == url)
        return;
    m_audioUrl = url;
    emit audioUrlChanged();
}

void VideoPlayer::setUserAgent(const QString &ua)
{
    if (m_userAgent == ua)
        return;
    m_userAgent = ua;
    emit userAgentChanged();
}

void VideoPlayer::play()
{
    if (!m_pipeline) {
        if (m_videoUrl.isEmpty()) {
            setError("no video url");
            return;
        }
        YLOG << "[youfish] play(): building pipeline"
                 << "\n  video:" << m_videoUrl.left(80)
                 << "\n  audio:" << m_audioUrl.left(80)
                 << "\n  ua:" << m_userAgent.left(50);
        m_prerollTimer.start();          // measure build->first PLAYING (buffering/preroll cost)
        m_prerollLogged = false;
        buildPipeline();
        if (!m_pipeline)
            return;
    }
    if (m_ended) {
        // Tapping play after the video finished replays it: a flush-seek to 0 clears
        // the EOS so data flows again.
        m_ended = false;
        sendSeek(0);
    }
    GstStateChangeReturn r = gst_element_set_state(m_pipeline, GST_STATE_PLAYING);
    YLOG << "[youfish] set_state(PLAYING) returned" << gst_element_state_change_return_get_name(r);
    m_posTimer->start();
    if (!m_playing) {
        m_playing = true;
        emit playingChanged();
    }
}

void VideoPlayer::pause()
{
    if (!m_pipeline)
        return;
    gst_element_set_state(m_pipeline, GST_STATE_PAUSED);
    m_playing = false;
    emit playingChanged();
}

void VideoPlayer::setVideoActive(bool active)
{
    if (m_videoActive == active)
        return;
    m_videoActive = active;
    // In muxed mode the one uridecodebin carries audio too, so freezing it would silence
    // background playback — leave it running (it keeps decoding video, a minor CPU cost).
    if (!m_pipeline || !m_videoBin || m_muxed)
        return;
    if (!active) {
        // Hidden: lock the video decoder in PAUSED so it stops decoding. The audio branch
        // is a separate uridecodebin/sink, so audio keeps playing without a hiccup.
        gst_element_set_locked_state(m_videoBin, TRUE);
        gst_element_set_state(m_videoBin, GST_STATE_PAUSED);
        YLOG << "[youfish] video branch frozen (app hidden)";
    } else {
        // Visible again: just bring the video branch back to the pipeline's current state
        // and let it catch up to the audio clock on its own. Do NOT flush-seek here: a
        // flush re-prerolls the video sink, which drags the whole pipeline to PAUSED and
        // silences the audio. Setting PAUSED->PLAYING resumes from the existing preroll with
        // no pipeline disturbance, so the audio never stops.
        GstState cur = GST_STATE_PLAYING;
        gst_element_get_state(m_pipeline, &cur, nullptr, 0);
        gst_element_set_locked_state(m_videoBin, FALSE);
        gst_element_set_state(m_videoBin, cur);
        YLOG << "[youfish] video branch resumed (app visible)";
    }
}

void VideoPlayer::requestRepaint()
{
    // Re-run paint() so the current frame is re-uploaded from CPU memory — recovers the video
    // after a display-off/on cycle drops or corrupts its GL texture (black/green frame on wake).
    update();
}

void VideoPlayer::setEqEnabled(bool on)
{
    if (m_eqEnabled == on)
        return;
    m_eqEnabled = on;
    applyEqBands();               // live if the element exists; otherwise applied at next build
}

void VideoPlayer::setEqBand(int index, double gainDb)
{
    if (index < 0 || index >= 10)
        return;
    if (gainDb < -24.0) gainDb = -24.0;
    if (gainDb > 12.0)  gainDb = 12.0;
    m_eqBands[index] = gainDb;
    if (m_eqEnabled)
        applyEqBands();
}

void VideoPlayer::applyEqBands()
{
    if (!m_equalizer)
        return;
    for (int i = 0; i < 10; ++i) {
        gchar name[8];
        g_snprintf(name, sizeof(name), "band%d", i);
        g_object_set(m_equalizer, name,
                     (gdouble)(m_eqEnabled ? m_eqBands[i] : 0.0), nullptr);
    }
}

void VideoPlayer::setBoost(double gain)
{
    if (gain < 1.0)  gain = 1.0;      // never attenuate here (that's the system volume's job)
    if (gain > 6.0)  gain = 6.0;      // +15.5 dB ceiling
    m_boostGain = gain;
    if (m_boost)
        g_object_set(m_boost, "volume", (gdouble)m_boostGain, nullptr);
}

void VideoPlayer::stop()
{
    teardown();
    m_ended = false;
    if (m_playing) {
        m_playing = false;
        emit playingChanged();
    }
    if (m_position != 0) { m_position = 0; emit positionChanged(); }
    if (m_duration != 0) { m_duration = 0; emit durationChanged(); }
}

void VideoPlayer::seek(qint64 positionMs)
{
    if (!m_pipeline || positionMs < 0)
        return;
    sendSeek(positionMs);
    if (m_ended) {
        // Scrubbing after the video finished resumes playback from the new position.
        m_ended = false;
        gst_element_set_state(m_pipeline, GST_STATE_PLAYING);
        m_posTimer->start();
        if (!m_playing) {
            m_playing = true;
            emit playingChanged();
        }
    }
    // Reflect the target right away so the scrubber doesn't snap back to the old
    // position before the next poll catches up.
    m_position = positionMs;
    emit positionChanged();
}

void VideoPlayer::sendSeek(qint64 positionMs)
{
    const gint64 t = (gint64)positionMs * GST_MSECOND;
    if (m_muxed) {
        // One source feeds both branches, so a normal pipeline-level seek reaches both
        // sinks and converges at the single uridecodebin — video + audio stay aligned.
        // KEY_UNIT is fine here: one demuxer seeks both tracks off the same keyframe, and
        // it makes the seek snappy.
        const GstSeekFlags mflags =
            (GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_KEY_UNIT);
        gboolean ok = m_pipeline && gst_element_seek(m_pipeline, m_rate, GST_FORMAT_TIME,
            mflags, GST_SEEK_TYPE_SET, t, GST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE);
        YLOG << "[youfish] muxed seek" << positionMs << "ms rate=" << m_rate << ":" << ok;
        return;
    }
    // Two independent sink branches (video appsink + audio pulsesink), each fed by its
    // own uridecodebin. A single pipeline-level seek only reaches one branch and returns
    // FALSE, so send the flush-seek to BOTH sinks — each carries it up its own branch to
    // that branch's source, keeping the two tracks aligned.
    // ACCURATE, *not* KEY_UNIT: the branches seek independently, so KEY_UNIT would snap the
    // video-only stream to its nearest keyframe (YouTube DASH GOPs are seconds apart) while
    // the audio stream lands ~exactly on t — leaving lip-sync off by up to a GOP after every
    // scrub. ACCURATE makes both land on the identical timestamp (the video decoder decodes
    // and discards up to t), so they stay aligned. The extra decode is trivial: both streams
    // are already downloadbuffered to disk, so it's a local seek + a few frames.
    // Carry m_rate on the seek so scrubbing keeps the chosen playback speed instead of
    // snapping back to 1.0. scaletempo on the audio branch keeps pitch natural.
    const GstSeekFlags dflags =
        (GstSeekFlags)(GST_SEEK_FLAG_FLUSH | GST_SEEK_FLAG_ACCURATE);
    gboolean vok = FALSE, aok = FALSE;
    if (m_videoSink)
        vok = gst_element_send_event(m_videoSink,
            gst_event_new_seek(m_rate, GST_FORMAT_TIME, dflags,
                GST_SEEK_TYPE_SET, t, GST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE));
    if (m_pulsesink)
        aok = gst_element_send_event(m_pulsesink,
            gst_event_new_seek(m_rate, GST_FORMAT_TIME, dflags,
                GST_SEEK_TYPE_SET, t, GST_SEEK_TYPE_NONE, GST_CLOCK_TIME_NONE));
    YLOG << "[youfish] seek" << positionMs << "ms rate=" << m_rate
         << ": video=" << vok << "audio=" << aok;
}

void VideoPlayer::setRate(qreal rate)
{
    if (rate <= 0.0)
        rate = 1.0;
    if (qFuzzyCompare(m_rate, rate))
        return;
    m_rate = rate;
    emit rateChanged();
    if (!m_pipeline)
        return;
    // Re-seek from the current spot so the new rate takes effect immediately.
    gint64 pos = 0;
    if (!gst_element_query_position(m_pipeline, GST_FORMAT_TIME, &pos) || pos < 0)
        pos = (gint64)m_position * GST_MSECOND;
    sendSeek(pos / GST_MSECOND);
}

void VideoPlayer::updatePosition()
{
    if (!m_pipeline)
        return;
    gint64 pos = 0, dur = 0;
    if (gst_element_query_position(m_pipeline, GST_FORMAT_TIME, &pos) && pos >= 0) {
        qint64 ms = pos / GST_MSECOND;
        if (ms != m_position) { m_position = ms; emit positionChanged(); }
    }
    // Duration may not be known until the demuxer has parsed the moov; keep polling.
    if (gst_element_query_duration(m_pipeline, GST_FORMAT_TIME, &dur) && dur > 0) {
        qint64 ms = dur / GST_MSECOND;
        if (ms != m_duration) { m_duration = ms; emit durationChanged(); }
    }
}

void VideoPlayer::buildPipeline()
{
    // Muxed mode: only a video URL was set (no separate audio) → it's a single stream
    // carrying both tracks, so one uridecodebin feeds both sink branches.
    m_muxed = m_audioUrl.isEmpty();
    // Reset the effective mode from the request each build so a fallback on one video (a codec
    // droidvdec can't handle) doesn't disable hw for the next. m_hwDecodeReq is the Settings
    // toggle, already OR'd with the YOUFISH_HWDEC env override at construction.
    m_hwDecode = m_hwDecodeReq;
    m_rateEngaged = false;   // re-engage the remembered speed on this fresh pipeline (at ASYNC_DONE)

    m_pipeline = gst_pipeline_new("youfish-player");
    m_videoBin = gst_element_factory_make("uridecodebin", "videosrc");
    m_audioBin = m_muxed ? nullptr : gst_element_factory_make("uridecodebin", "audiosrc");
    m_scaletempo = gst_element_factory_make("scaletempo", "stempo");
    m_pulsesink = gst_element_factory_make("pulsesink", "asink");

    // Video branch is mode-dependent. Hardware: a single droideglsink that the decoded video
    // pad links straight into (droidvdec auto-plugs upstream once caps negotiate). Software:
    // the classic videoconvert->appsink pair. m_videoInput is what the pad links to;
    // m_videoSink is the branch tail we address for seeks.
    if (m_hwDecode) {
        // Explicit hardware chain (matches the proven standalone pipeline): uridecodebin stops
        // at the parsed encoded stream, then WE plug droidvdec -> droideglsink. This sidesteps
        // decodebin's autoplug, which won't reliably pick droidvdec even though
        // it outranks the software decoder (257 > 256) and works when instantiated directly.
        {
            QMutexLocker hwLock(&m_hwMutex);        // publish m_hw under the same lock paint() reads
            m_hw = new HwVideoSink(this);
        }
        connect(m_hw, &HwVideoSink::updateRequested, this, [this]{ update(); });
        m_videoSink = m_hw->sinkElement();          // droideglsink
        m_hwDec = gst_element_factory_make("droidvdec", "hwdec");
        if (!m_videoSink || !m_hwDec) {
            qWarning() << "[youfish] droidvdec/droideglsink unavailable — software fallback";
            if (m_hwDec) { gst_object_unref(m_hwDec); m_hwDec = nullptr; }
            {
                QMutexLocker hwLock(&m_hwMutex);
                delete m_hw;
                m_hw = nullptr;
            }
            m_videoSink = nullptr;
            m_hwDecode = false;
        } else {
            m_videoInput = m_hwDec;                  // encoded video pad links into droidvdec
        }
    }
    if (!m_hwDecode) {
        m_videoConvert = gst_element_factory_make("videoconvert", "vconv");
        m_appsink = gst_element_factory_make("appsink", "vsink");
        m_videoInput = m_videoConvert;
        m_videoSink = m_appsink;
    }

    if (!m_pipeline || !m_videoBin || (!m_muxed && !m_audioBin) ||
        !m_scaletempo || !m_pulsesink || !m_videoInput || !m_videoSink) {
        qWarning() << "[youfish] element creation failed:"
                   << "pipeline" << (bool)m_pipeline << "vbin" << (bool)m_videoBin
                   << "abin" << (bool)m_audioBin << "vinput" << (bool)m_videoInput
                   << "vsink" << (bool)m_videoSink << "stempo" << (bool)m_scaletempo
                   << "pulse" << (bool)m_pulsesink;
        setError("failed to create gstreamer elements");
        teardown();
        return;
    }

    const QByteArray videoUri = m_videoUrl.toUtf8();
    const QByteArray audioUri = m_audioUrl.toUtf8();
    g_object_set(m_videoBin, "uri", videoUri.constData(), nullptr);
    if (m_audioBin)
        g_object_set(m_audioBin, "uri", audioUri.constData(), nullptr);

    // Hardware mode: steer decodebin to the hardware decoder. Without this, uridecodebin
    // decodes to plain system-memory video/x-raw and picks a *software* decoder (avdec_h264)
    // by rank; its frames can't feed droideglsink → "not-negotiated". Telling uridecodebin to
    // stop at droid graphic-buffer memory forces it to plug droidvdec (the only decoder that
    // produces those buffers). The video-only bin needs just the droid video caps; the muxed
    // bin also decodes its audio track to raw.
    if (m_hwDecode) {
        // Stop uridecodebin at the parsed *encoded* stream so it does source+demux+parse only;
        // droidvdec (plugged explicitly below) does the actual decode. avc/au gives droidvdec
        // the codec_data it wants (exactly the caps it got in the working standalone test).
        // Muxed streams still decode their audio track to raw.
        GstCaps *stopCaps = gst_caps_from_string(
            m_muxed ? "video/x-h264, stream-format=(string)avc, alignment=(string)au;"
                      " video/x-vp9; audio/x-raw"
                    : "video/x-h264, stream-format=(string)avc, alignment=(string)au;"
                      " video/x-vp9");
        g_object_set(m_videoBin, "caps", stopCaps, nullptr);
        gst_caps_unref(stopCaps);
        YLOG << "[youfish] hw: uridecodebin stops at encoded video; droidvdec is explicit";
    }

    // In hardware mode we WANT droidvdec, so never force software there. In software mode,
    // force it so the decoded frames are CPU-mappable for the appsink.
    if (!m_hwDecode &&
        g_object_class_find_property(G_OBJECT_GET_CLASS(m_videoBin), "force-sw-decoders")) {
        g_object_set(m_videoBin, "force-sw-decoders", TRUE, nullptr);
        YLOG << "[youfish] force-sw-decoders set on video";
    }
    // A local file (downloaded video) is already on disk: filesrc is random-access, so qtdemux
    // seeks natively in pull mode with no buffering. The network-oriented buffering below only
    // slows a local preroll down, so skip it entirely for file:// sources.
    const bool localFile = m_videoUrl.startsWith(QLatin1String("file://"));

    if (!localFile) {
        g_object_set(m_videoBin, "buffer-duration", (gint64)(3 * GST_SECOND), nullptr);
        if (m_audioBin)
            g_object_set(m_audioBin, "buffer-duration", (gint64)(3 * GST_SECOND), nullptr);

        // Buffer the network stream to a temp file (downloadbuffer) rather than a memory
        // ring. That gives the demuxer random access, so qtdemux activates in PULL mode and
        // can actually seek — in push mode it flatly "ignores seek in push mode".
        if (g_object_class_find_property(G_OBJECT_GET_CLASS(m_videoBin), "download")) {
            g_object_set(m_videoBin, "download", TRUE, nullptr);
            if (m_audioBin)
                g_object_set(m_audioBin, "download", TRUE, nullptr);
            YLOG << "[youfish] download buffering enabled (for seek)";
        } else {
            YLOG << "[youfish] no download property — seek may stay push-mode";
        }
    } else {
        YLOG << "[youfish] local file — skipping network buffering (native pull-mode seek)";
    }

    // Software video sink needs RGBA caps + the appsink callback wiring. The hardware sink
    // (droideglsink) consumes native graphic buffers directly and pushes frames via its
    // show-frame signal, so it needs none of this.
    if (!m_hwDecode) {
        GstCaps *caps = gst_caps_new_simple("video/x-raw",
                                            "format", G_TYPE_STRING, "RGBA", nullptr);
        gst_app_sink_set_caps(GST_APP_SINK(m_appsink), caps);
        gst_caps_unref(caps);
        // sync=TRUE clocks frame presentation to the pipeline clock, so video runs at real
        // time and stays aligned with the audio sink (which owns the clock). With sync=FALSE
        // the appsink emitted every frame the instant it decoded — the "super speed" playback.
        // qos=TRUE is what keeps lip-sync under load: the sink measures how late each frame is
        // and sends QoS upstream, so a decoder that can't sustain real time (e.g. 1080p60 in
        // software) SKIPS late frames to catch back up to the audio clock instead of decoding
        // every one and drifting progressively behind it. drop=TRUE + max-buffers=2 caps the
        // sink queue so a slow *paint* sheds old frames rather than back-pressuring the
        // pipeline. Net: heavy decode degrades by dropping/skipping frames, never by desyncing.
        g_object_set(m_appsink,
                     "emit-signals", TRUE,
                     "sync", TRUE,
                     "qos", TRUE,
                     "max-buffers", (guint)2,
                     "drop", TRUE,
                     nullptr);
        g_signal_connect(m_appsink, "new-sample", G_CALLBACK(onNewSample), this);
    }

    // Add the common elements first (audio path + video source). gst_bin_add_many stops at
    // the first nullptr, so add the optional audio bin separately.
    gst_bin_add_many(GST_BIN(m_pipeline), m_videoBin, m_scaletempo, m_pulsesink, nullptr);
    if (m_audioBin)
        gst_bin_add(GST_BIN(m_pipeline), m_audioBin);

    // Video branch: hardware = droidvdec -> droideglsink (both explicit); software =
    // videoconvert -> appsink.
    if (m_hwDecode) {
        gst_bin_add_many(GST_BIN(m_pipeline), m_hwDec, m_videoSink, nullptr);
        if (!gst_element_link(m_hwDec, m_videoSink)) {
            qWarning() << "[youfish] link droidvdec -> droideglsink FAILED";
            setError("link droidvdec -> droideglsink failed");
        }
    } else {
        gst_bin_add_many(GST_BIN(m_pipeline), m_videoConvert, m_appsink, nullptr);
        if (!gst_element_link(m_videoConvert, m_appsink)) {
            qWarning() << "[youfish] link videoconvert -> appsink FAILED";
            setError("link videoconvert -> appsink failed");
        }
    }

    // Audio effects chain, spliced between scaletempo and the sink:
    //   scaletempo -> [eqin -> equalizer -> eqout] -> [dynConv -> boost -> limiter] -> pulsesink
    // Every stage is optional (graceful fallback when a plugin is missing); at worst audio flows
    // scaletempo -> pulsesink so playback always works. Effects stay inserted even when neutral
    // (flat EQ / 1.0 boost are transparent) so they can be toggled and adjusted live.
    // GST_OBJECT_PARENT reads ownership without taking a ref (unlike gst_element_get_parent).
    GstElement *prev = m_scaletempo;
    auto chainAdd = [&](GstElement *e) {
        if (!GST_OBJECT_PARENT(e))
            gst_bin_add(GST_BIN(m_pipeline), e);
        if (!gst_element_link(prev, e)) {
            qWarning() << "[youfish] audio chain link failed at" << GST_ELEMENT_NAME(e);
            setError("audio chain link failed");
        }
        prev = e;
    };

    // 10-band equalizer.
    m_equalizer = gst_element_factory_make("equalizer-10bands", "eq");
    if (m_equalizer) {
        m_eqConvIn = gst_element_factory_make("audioconvert", "eqin");
        m_eqConvOut = gst_element_factory_make("audioconvert", "eqout");
    }
    if (m_equalizer && m_eqConvIn && m_eqConvOut) {
        chainAdd(m_eqConvIn);
        chainAdd(m_equalizer);
        chainAdd(m_eqConvOut);
        applyEqBands();
    } else {
        for (GstElement **e : { &m_equalizer, &m_eqConvIn, &m_eqConvOut }) {
            if (*e && !GST_OBJECT_PARENT(*e))
                gst_object_unref(*e);
            *e = nullptr;
        }
    }

    // Volume boost + soft limiter. audioconvert normalises the format for the limiter; volume
    // applies the boost; rglimiter (optional) soft-limits the boosted peaks so exceeding the
    // system max doesn't hard-clip. Without the limiter plugin, boost still works but can clip
    // when pushed hard.
    m_dynConv = gst_element_factory_make("audioconvert", "dynin");
    m_boost = gst_element_factory_make("volume", "boost");
    if (m_dynConv && m_boost) {
        chainAdd(m_dynConv);
        g_object_set(m_boost, "volume", (gdouble)m_boostGain, nullptr);
        chainAdd(m_boost);
        m_limiter = gst_element_factory_make("rglimiter", "lim");
        if (m_limiter)
            chainAdd(m_limiter);
    } else {
        for (GstElement **e : { &m_dynConv, &m_boost }) {
            if (*e && !GST_OBJECT_PARENT(*e))
                gst_object_unref(*e);
            *e = nullptr;
        }
    }

    if (!gst_element_link(prev, m_pulsesink)) {
        qWarning() << "[youfish] link audio chain -> pulsesink FAILED";
        setError("link to pulsesink failed");
    }
    YLOG << "[youfish] audio chain: eq=" << (bool)m_equalizer
         << " boost=" << (bool)m_boost << " limiter=" << (bool)m_limiter;

    g_signal_connect(m_videoBin, "source-setup", G_CALLBACK(onSourceSetup), this);
    if (m_muxed) {
        // One source, both tracks → route video and audio pads from the same uridecodebin.
        g_signal_connect(m_videoBin, "pad-added", G_CALLBACK(onMuxedPad), this);
    } else {
        g_signal_connect(m_audioBin, "source-setup", G_CALLBACK(onSourceSetup), this);
        g_signal_connect(m_videoBin, "pad-added", G_CALLBACK(onVideoPad), this);
        g_signal_connect(m_audioBin, "pad-added", G_CALLBACK(onAudioPad), this);
    }

    GstBus *bus = gst_element_get_bus(m_pipeline);
    m_busWatch = gst_bus_add_watch(bus, onBusMessage, this);
    gst_object_unref(bus);
    YLOG << "[youfish] pipeline built, bus watch id" << m_busWatch;
}

void VideoPlayer::teardown()
{
    if (m_posTimer)
        m_posTimer->stop();
    if (m_busWatch) {
        g_source_remove(m_busWatch);
        m_busWatch = 0;
    }
    // Unlock the video branch first — if it was frozen (locked PAUSED) it wouldn't follow
    // the pipeline to NULL and could hang teardown.
    if (m_videoBin)
        gst_element_set_locked_state(m_videoBin, FALSE);
    // Free any element that was created but never added to a bin. buildPipeline()'s failure paths
    // (a missing plugin → early teardown) leave the created elements standalone with a floating
    // ref that the pipeline unref below can't reclaim → a leak. An element already added to the
    // pipeline has a parent, so the guard skips it (it's freed with the pipeline). The aliases
    // m_videoInput/m_videoSink point at elements already in this list, so they're intentionally
    // excluded to avoid a double-unref. Done BEFORE the pipeline unref, while parents are intact.
    GstElement *owned[] = { m_videoBin, m_audioBin, m_scaletempo, m_pulsesink,
                            m_videoConvert, m_appsink, m_hwDec };
    for (GstElement *e : owned) {
        if (e && !GST_OBJECT_PARENT(e))
            gst_object_unref(e);
    }
    if (m_pipeline) {
        gst_element_set_state(m_pipeline, GST_STATE_NULL);
        gst_object_unref(m_pipeline);
        m_pipeline = nullptr;
    }
    // Dropping the pipeline released the droideglsink; its toggle-ref fired cleanup() so the
    // renderer detached from GStreamer cleanly. Now delete the renderer object itself — under
    // m_hwMutex so we can't free it while paint() is dereferencing m_hw on the render thread.
    {
        QMutexLocker hwLock(&m_hwMutex);
        if (m_hw) {
            delete m_hw;
            m_hw = nullptr;
        }
    }
    m_videoBin = m_audioBin = m_videoConvert = nullptr;
    m_appsink = m_scaletempo = m_pulsesink = nullptr;
    m_equalizer = m_eqConvIn = m_eqConvOut = nullptr;
    m_dynConv = m_boost = m_limiter = nullptr;
    m_videoInput = m_videoSink = m_hwDec = nullptr;
    m_videoActive = true;
    m_muxed = false;
}

void VideoPlayer::setError(const QString &message)
{
    qWarning() << "[youfish] ERROR:" << message;
    emit errorOccurred(message);
}

void VideoPlayer::onSourceSetup(GstElement *, GstElement *source, gpointer self)
{
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    GObjectClass *klass = G_OBJECT_GET_CLASS(source);
    YLOG << "[youfish] source-setup on" << GST_ELEMENT_NAME(source);
    if (g_object_class_find_property(klass, "user-agent")) {
        QByteArray ua = player->m_userAgent.toUtf8();
        if (ua.isEmpty())
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36";
        g_object_set(source, "user-agent", ua.constData(), nullptr);
        YLOG << "[youfish]   set user-agent:" << ua.left(50);
    }
    if (g_object_class_find_property(klass, "timeout"))
        g_object_set(source, "timeout", (guint)30, nullptr);
}

static bool padHasMediaPrefix(GstPad *pad, const char *prefix)
{
    GstCaps *caps = gst_pad_get_current_caps(pad);
    if (!caps)
        caps = gst_pad_query_caps(pad, nullptr);
    bool match = false;
    if (caps) {
        // query_caps can legitimately return empty caps (0 structures); guard before reading [0]
        // so gst_caps_get_structure() doesn't hand a null to gst_structure_get_name().
        if (gst_caps_get_size(caps) > 0) {
            const gchar *name = gst_structure_get_name(gst_caps_get_structure(caps, 0));
            match = name && g_str_has_prefix(name, prefix);
        }
        gst_caps_unref(caps);
    }
    return match;
}

void VideoPlayer::onVideoPad(GstElement *, GstPad *pad, gpointer self)
{
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    bool isVideo = padHasMediaPrefix(pad, "video/");
    YLOG << "[youfish] video uridecodebin pad-added, isVideo=" << isVideo;
    if (!isVideo)
        return;
    GstPad *sink = gst_element_get_static_pad(player->m_videoInput, "sink");
    GstPadLinkReturn r = GST_PAD_LINK_OK;
    if (sink && !gst_pad_is_linked(sink))
        r = gst_pad_link(pad, sink);
    YLOG << "[youfish]   linked video pad ->" << gst_pad_link_get_name(r);
    if (sink)
        gst_object_unref(sink);
}

void VideoPlayer::onAudioPad(GstElement *, GstPad *pad, gpointer self)
{
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    bool isAudio = padHasMediaPrefix(pad, "audio/");
    YLOG << "[youfish] audio uridecodebin pad-added, isAudio=" << isAudio;
    if (!isAudio)
        return;
    GstPad *sink = gst_element_get_static_pad(player->m_scaletempo, "sink");
    GstPadLinkReturn r = GST_PAD_LINK_OK;
    if (sink && !gst_pad_is_linked(sink))
        r = gst_pad_link(pad, sink);
    YLOG << "[youfish]   linked audio pad ->" << gst_pad_link_get_name(r);
    if (sink)
        gst_object_unref(sink);
}

void VideoPlayer::onMuxedPad(GstElement *, GstPad *pad, gpointer self)
{
    // One uridecodebin exposes both a video and an audio pad; send each to its own sink
    // branch (video → videoconvert → appsink, audio → scaletempo → pulsesink).
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    GstElement *target = nullptr;
    if (padHasMediaPrefix(pad, "video/"))
        target = player->m_videoInput;
    else if (padHasMediaPrefix(pad, "audio/"))
        target = player->m_scaletempo;
    YLOG << "[youfish] muxed uridecodebin pad-added, target="
         << (target == player->m_videoInput ? "video" : target ? "audio" : "none");
    if (!target)
        return;
    GstPad *sink = gst_element_get_static_pad(target, "sink");
    GstPadLinkReturn r = GST_PAD_LINK_OK;
    if (sink && !gst_pad_is_linked(sink))
        r = gst_pad_link(pad, sink);
    YLOG << "[youfish]   linked muxed pad ->" << gst_pad_link_get_name(r);
    if (sink)
        gst_object_unref(sink);
}

GstFlowReturn VideoPlayer::onNewSample(GstElement *sink, gpointer self)
{
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    GstSample *sample = gst_app_sink_pull_sample(GST_APP_SINK(sink));
    if (!sample)
        return GST_FLOW_OK;

    GstCaps *caps = gst_sample_get_caps(sample);
    GstVideoInfo info;
    if (caps && gst_video_info_from_caps(&info, caps)) {
        GstBuffer *buffer = gst_sample_get_buffer(sample);
        GstVideoFrame frame;
        if (buffer && gst_video_frame_map(&frame, &info, buffer, GST_MAP_READ)) {
            const int w = GST_VIDEO_FRAME_WIDTH(&frame);
            const int h = GST_VIDEO_FRAME_HEIGHT(&frame);
            const int stride = GST_VIDEO_FRAME_PLANE_STRIDE(&frame, 0);
            const uchar *data =
                static_cast<const uchar *>(GST_VIDEO_FRAME_PLANE_DATA(&frame, 0));
            static int frameCount = 0;
            if (frameCount++ == 0)
                YLOG << "[youfish] FIRST video frame" << w << "x" << h << "stride" << stride;
            QImage image(data, w, h, stride, QImage::Format_RGBA8888);
            QImage copy = image.copy();  // detach before unmap
            gst_video_frame_unmap(&frame);
            {
                QMutexLocker lock(&player->m_frameMutex);
                player->m_frame = copy;
            }
            QMetaObject::invokeMethod(player, "update", Qt::QueuedConnection);
        }
    }
    gst_sample_unref(sample);
    return GST_FLOW_OK;
}

gboolean VideoPlayer::onBusMessage(GstBus *, GstMessage *msg, gpointer self)
{
    VideoPlayer *player = static_cast<VideoPlayer *>(self);
    const gchar *src = GST_MESSAGE_SRC(msg) ? GST_OBJECT_NAME(GST_MESSAGE_SRC(msg)) : "?";

    switch (GST_MESSAGE_TYPE(msg)) {
    case GST_MESSAGE_ERROR: {
        GError *err = nullptr;
        gchar *debug = nullptr;
        gst_message_parse_error(msg, &err, &debug);
        qWarning() << "[youfish] BUS ERROR from" << src << ":"
                   << (err ? err->message : "?") << "| debug:" << (debug ? debug : "");
        player->setError(QString::fromUtf8(err ? err->message : "gstreamer error"));
        if (err)
            g_error_free(err);
        g_free(debug);
        break;
    }
    case GST_MESSAGE_WARNING: {
        GError *err = nullptr;
        gchar *debug = nullptr;
        gst_message_parse_warning(msg, &err, &debug);
        qWarning() << "[youfish] BUS WARNING from" << src << ":"
                   << (err ? err->message : "?") << "| debug:" << (debug ? debug : "");
        if (err)
            g_error_free(err);
        g_free(debug);
        break;
    }
    case GST_MESSAGE_EOS:
        YLOG << "[youfish] BUS EOS";
        player->m_ended = true;   // next play()/seek() replays instead of no-op
        if (player->m_playing) {
            player->m_playing = false;
            emit player->playingChanged();
        }
        emit player->ended();     // let QML roll an autoplay queue on to the next video
        break;
    case GST_MESSAGE_STATE_CHANGED:
        if (GST_MESSAGE_SRC(msg) == GST_OBJECT(player->m_pipeline)) {
            GstState olds, news;
            gst_message_parse_state_changed(msg, &olds, &news, nullptr);
            YLOG << "[youfish] pipeline state" << gst_element_state_get_name(olds)
                     << "->" << gst_element_state_get_name(news);
            if (news == GST_STATE_PLAYING && !player->m_prerollLogged
                    && player->m_prerollTimer.isValid()) {
                player->m_prerollLogged = true;   // first PLAYING after a build = the preroll cost
                YLOG << "[youfish/t] preroll (build->PLAYING)"
                         << player->m_prerollTimer.elapsed() << "ms";
            }
        }
        break;
    case GST_MESSAGE_ASYNC_DONE:
        // Preroll finished — both sink branches are ready, but no PLAYING frame has reached the
        // appsink yet (it only pushes new-sample in PLAYING). If a non-default speed is set, engage
        // it ONCE here: the flush-seek reaches BOTH branches together (fixing the audio-at-one-rate
        // / video-at-another split from applying it mid-preroll) and lands before the first visible
        // frame, so there's no dark flash. The seek's own later ASYNC_DONE is skipped by the flag.
        if (!player->m_rateEngaged && !qFuzzyCompare(player->m_rate, 1.0)
                && GST_MESSAGE_SRC(msg) == GST_OBJECT(player->m_pipeline)) {
            player->m_rateEngaged = true;
            gint64 pos = 0;
            if (!gst_element_query_position(player->m_pipeline, GST_FORMAT_TIME, &pos) || pos < 0)
                pos = 0;
            player->sendSeek(pos / GST_MSECOND);
            YLOG << "[youfish] engaged start speed x" << player->m_rate;
        }
        break;
    case GST_MESSAGE_BUFFERING: {
        gint percent = 0;
        gst_message_parse_buffering(msg, &percent);
        YLOG << "[youfish] buffering" << percent << "% (from" << src << ")";
        break;
    }
    default:
        YLOG << "[youfish] bus msg" << GST_MESSAGE_TYPE_NAME(msg) << "from" << src;
        break;
    }
    return TRUE;
}

void VideoPlayer::paint(QPainter *painter)
{
    // Hardware path: draw the decoded EGLImage straight into the item's FBO via native GL.
    // The renderer letterboxes internally; we just clear to black and hand it the transform.
    // Held under m_hwMutex so teardown() on the GUI thread can't delete m_hw mid-draw (UAF).
    // Scoped so the lock is released before the software path takes m_frameMutex (no nesting).
    {
        QMutexLocker hwLock(&m_hwMutex);
        if (m_hw) {
            painter->fillRect(boundingRect(), Qt::black);
            m_hw->resize(boundingRect().size());
            if (m_hw->hasFrame()) {
                painter->beginNativePainting();
                m_hw->paint(QMatrix4x4(painter->combinedTransform()), painter->viewport());
                painter->endNativePainting();
            }
            return;
        }
    }

    QImage frame;
    {
        QMutexLocker lock(&m_frameMutex);
        frame = m_frame;
    }
    painter->fillRect(boundingRect(), Qt::black);
    if (frame.isNull())
        return;

    QSize scaled = frame.size().scaled(boundingRect().size().toSize(),
                                       Qt::KeepAspectRatio);
    QRectF target((width() - scaled.width()) / 2.0,
                  (height() - scaled.height()) / 2.0,
                  scaled.width(), scaled.height());
    painter->drawImage(target, frame);
}
