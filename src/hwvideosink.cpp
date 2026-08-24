#include "hwvideosink.h"

#include <QDebug>
#include <QOpenGLContext>
#include <QtOpenGL/QGLShaderProgram>
#include <gst/video/video.h>
#include <gst/interfaces/nemoeglimagememory.h>

// Verbose tracing shares the app-wide YOUFISH_DEBUG gate used by videoplayer.cpp.
static const bool kHwDebug = qEnvironmentVariableIsSet("YOUFISH_DEBUG");
#define HWLOG if (kHwDebug) qDebug()

// Vertex passes item-pixel coords through the painter transform (matrixWorld) then the
// viewport ortho (matrix); external-OES sampler reads the native buffer straight through.
static const char *kVertexShader =
    "attribute vec4 inputVertex;\n"
    "attribute lowp vec2 textureCoord;\n"
    "uniform mat4 matrix;\n"
    "uniform mat4 matrixWorld;\n"
    "varying lowp vec2 fragTexCoord;\n"
    "void main() {\n"
    "    gl_Position = matrix * matrixWorld * inputVertex;\n"
    "    fragTexCoord = textureCoord;\n"
    "}\n";

static const char *kFragmentShader =
    "#extension GL_OES_EGL_image_external : enable\n"
    "uniform samplerExternalOES texture0;\n"
    "varying lowp vec2 fragTexCoord;\n"
    "void main() {\n"
    "    gl_FragColor = texture2D(texture0, fragTexCoord);\n"
    "}\n";

// Texture names whose owning HwVideoSink was torn down on the GUI thread (no current GL
// context, so glDeleteTextures couldn't run there). Drained on the next render-thread paint of
// ANY HwVideoSink — the GL context is shared, so any instance can free them. Without this, GL
// texture objects would accumulate across a session of videos.
static QMutex s_orphanMutex;
static std::vector<GLuint> s_orphanTextures;

static void drainOrphanTextures()
{
    std::vector<GLuint> ids;
    {
        QMutexLocker lock(&s_orphanMutex);
        ids.swap(s_orphanTextures);
    }
    if (!ids.empty())
        glDeleteTextures((GLsizei)ids.size(), ids.data());
}

HwVideoSink::HwVideoSink(QObject *parent)
    : QObject(parent)
{
    // Quad as a TRIANGLE_FAN: bottom-left, bottom-right, top-right, top-left.
    m_texCoords = { 0.f, 0.f,  1.f, 0.f,  1.f, 1.f,  0.f, 1.f };
    m_verts     = { 0.f, 0.f,  0.f, 0.f,  0.f, 0.f,  0.f, 0.f };
}

HwVideoSink::~HwVideoSink()
{
    cleanup();                      // no-op if the sink already detached via sink_notify
    if (m_queued)  gst_buffer_unref(m_queued);
    if (m_current) gst_buffer_unref(m_current);
    delete m_program;
}

GstElement *HwVideoSink::sinkElement()
{
    if (m_sink)
        return m_sink;

    m_sink = gst_element_factory_make("droideglsink", "hwvsink");
    if (!m_sink) {
        qWarning() << "[youfish/hw] failed to create droideglsink";
        return nullptr;
    }

    // Toggle-ref: when the pipeline drops its ref (teardown), sink_notify fires with
    // is_last_ref=TRUE and we detach cleanly. Mirrors gst-droid's RendererNemo.
    g_object_add_toggle_ref(G_OBJECT(m_sink), (GToggleNotify)sink_notify, this);
    m_displaySet = false;

    m_showFrameId   = g_signal_connect(m_sink, "show-frame",
                                       G_CALLBACK(show_frame), this);
    m_invalidatedId = g_signal_connect(m_sink, "buffers-invalidated",
                                       G_CALLBACK(buffers_invalidated), this);

    m_sinkPad = gst_element_get_static_pad(m_sink, "sink");
    if (m_sinkPad)
        m_notifyId = g_signal_connect(m_sinkPad, "notify::caps",
                                      G_CALLBACK(sink_caps_changed), this);

    HWLOG << "[youfish/hw] droideglsink created";
    return m_sink;
}

bool HwVideoSink::ensureGl()
{
    if (m_glEGLImageTargetTexture2DOES)
        return true;

    QOpenGLContext *ctx = QOpenGLContext::currentContext();
    if (!ctx) {
        qWarning() << "[youfish/hw] paint() with no current GL context";
        return false;
    }
    if (!ctx->hasExtension(QByteArrayLiteral("GL_OES_EGL_image_external"))) {
        qWarning() << "[youfish/hw] GL_OES_EGL_image_external not supported";
        return false;
    }
    m_glEGLImageTargetTexture2DOES = reinterpret_cast<PFNGLEGLIMAGETARGETTEXTURE2DOESPROC>(
        eglGetProcAddress("glEGLImageTargetTexture2DOES"));
    if (!m_glEGLImageTargetTexture2DOES) {
        qWarning() << "[youfish/hw] glEGLImageTargetTexture2DOES unresolved";
        return false;
    }
    return true;
}

void HwVideoSink::ensureProgram()
{
    if (m_program)
        return;

    m_program = new QGLShaderProgram;
    if (!m_program->addShaderFromSourceCode(QGLShader::Vertex, kVertexShader))
        qWarning() << "[youfish/hw] vertex shader:" << m_program->log();
    if (!m_program->addShaderFromSourceCode(QGLShader::Fragment, kFragmentShader))
        qWarning() << "[youfish/hw] fragment shader:" << m_program->log();
    m_program->bindAttributeLocation("inputVertex", 0);
    m_program->bindAttributeLocation("textureCoord", 1);
    if (!m_program->link())
        qWarning() << "[youfish/hw] program link:" << m_program->log();
}

void HwVideoSink::handleContextChange()
{
    // The GL context we built our resources in was destroyed (display blank with a
    // non-persistent view). Its shader program and texture NAMES are already gone — drop them
    // WITHOUT GL deletes, since deleting those stale names in the NEW context would clobber
    // unrelated objects. The EGLImages are display-scoped (not context-scoped), so we still
    // destroy them to avoid a leak; ditto the pinned GstMemory. Everything rebuilds lazily on
    // the next paint against the fresh context.
    delete m_program;                 // QGLShaderProgram's guard no-ops GL on the dead context
    m_program = nullptr;

    static const PFNEGLDESTROYIMAGEKHRPROC eglDestroyImageKHR =
        reinterpret_cast<PFNEGLDESTROYIMAGEKHRPROC>(eglGetProcAddress("eglDestroyImageKHR"));
    for (CachedTexture &t : m_textures) {
        if (eglDestroyImageKHR && m_dpy != EGL_NO_DISPLAY)
            eglDestroyImageKHR(m_dpy, t.image);
        gst_memory_unref(t.memory);   // NB: no glDeleteTextures — the name died with the context
    }
    m_textures.clear();

    m_glEGLImageTargetTexture2DOES = nullptr;  // re-resolve against the new context in ensureGl()
    m_displaySet = false;                      // re-push egl-display to the sink for the new ctx
    m_dpy = EGL_NO_DISPLAY;
    m_needGeom = true;                          // re-emit letterbox geometry after the rebuild
}

bool HwVideoSink::paint(const QMatrix4x4 &matrix, const QRectF &viewport)
{
    // A display blank destroys the GL context (the QQuickView is non-persistent), taking our
    // linked shader program and EGLImage textures with it. QGLShaderProgram keeps reporting
    // isLinked()==true afterwards, so paint() would draw with a dead program — nothing renders
    // (black frame) and every setUniformValue spams "shader program is not linked". Detect the
    // swap to a new context and drop the dead resources so they rebuild below.
    QOpenGLContext *ctx = QOpenGLContext::currentContext();
    if (ctx && ctx != m_glCtx) {
        // Rebuild if we still hold resources from the PREVIOUS context. Key this off m_program,
        // NOT m_glCtx: m_glCtx is a QPointer that auto-nulls the moment the old context is
        // destroyed, so by the time the new context arrives it's already null — testing it would
        // wrongly skip the rebuild (that was the bug that left the program dead → black + spam).
        if (m_program || !m_textures.isEmpty()) {
            HWLOG << "[youfish/hw] GL context changed — rebuilding shader + textures";
            handleContextChange();
        }
        m_glCtx = ctx;
    }

    if (!ensureGl())
        return false;

    drainOrphanTextures();   // free texture names deferred from a GUI-thread teardown

    if (m_dpy == EGL_NO_DISPLAY)
        m_dpy = eglGetCurrentDisplay();
    // The sink wants the render thread's EGLDisplay; only valid to push once we have it.
    if (m_sink && m_dpy != EGL_NO_DISPLAY && !m_displaySet) {
        g_object_set(G_OBJECT(m_sink), "egl-display", m_dpy, nullptr);
        m_displaySet = true;
    }
    if (m_dpy == EGL_NO_DISPLAY) {
        qWarning() << "[youfish/hw] no EGL display";
        return false;
    }

    // Hold the frame lock across the whole draw: this is what stops GStreamer unref-ing the
    // buffer we're sampling. Do NOT "optimise" it away.
    QMutexLocker lock(&m_mutex);
    if (!m_queued && !m_current)
        return false;

    ensureProgram();
    if (!m_program || !m_program->isLinked())
        return false;

    m_ortho = QMatrix4x4();
    m_ortho.ortho(viewport);

    paintFrame(matrix);
    return true;
}

void HwVideoSink::paintFrame(const QMatrix4x4 &matrix)
{
    if (m_invalidated) {
        m_invalidated = false;
        destroyCachedTextures();
    }

    GstBuffer *toRelease = nullptr;
    if (m_current != m_queued && m_bufferChanged) {
        toRelease = m_current;
        m_current = m_queued ? gst_buffer_ref(m_queued) : nullptr;
    }
    m_bufferChanged = false;

    if (!m_current || gst_buffer_n_memory(m_current) == 0) {
        if (toRelease) gst_buffer_unref(toRelease);
        return;
    }

    if (m_needGeom)
        recalcGeometry();

    GstMemory *memory = gst_buffer_peek_memory(m_current, 0);
    GLuint texture = 0;

    glActiveTexture(GL_TEXTURE0);

    for (const CachedTexture &c : m_textures) {
        if (c.memory == memory) {
            texture = c.textureId;
            glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture);
            m_glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, c.image);
            break;
        }
    }

    if (texture == 0) {
        if (EGLImageKHR img = nemo_gst_egl_image_memory_create_image(memory, m_dpy, nullptr)) {
            glGenTextures(1, &texture);
            glBindTexture(GL_TEXTURE_EXTERNAL_OES, texture);
            // FIX vs RendererNemo: external-OES has no mipmaps and MAG accepts only
            // NEAREST/LINEAR; the original set GL_NEAREST_MIPMAP_LINEAR (an INVALID_ENUM).
            glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_EXTERNAL_OES, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            m_glEGLImageTargetTexture2DOES(GL_TEXTURE_EXTERNAL_OES, (GLeglImageOES)img);
            m_textures.push_back({ gst_memory_ref(memory), img, texture });
        }
    }

    if (texture == 0) {
        // No droid memory -> a software decoder was auto-plugged instead of droidvdec (the
        // codec isn't hardware-decodable). Nothing to draw here; caller stays on black.
        if (toRelease) gst_buffer_unref(toRelease);
        return;
    }

    m_program->bind();
    m_program->setUniformValue("texture0", 0);
    m_program->setUniformValue("matrix", m_ortho);
    m_program->setUniformValue("matrixWorld", matrix);
    m_program->enableAttributeArray(0);
    m_program->enableAttributeArray(1);
    m_program->setAttributeArray(0, m_verts.data(), 2);
    m_program->setAttributeArray(1, m_texCoords.data(), 2);

    glDrawArrays(GL_TRIANGLE_FAN, 0, 4);

    m_program->disableAttributeArray(0);
    m_program->disableAttributeArray(1);
    m_program->release();
    glBindTexture(GL_TEXTURE_EXTERNAL_OES, 0);

    if (toRelease)
        gst_buffer_unref(toRelease);
}

void HwVideoSink::recalcGeometry()
{
    m_needGeom = false;
    if (!m_size.isValid() || !m_videoSize.isValid())
        return;

    const QRectF a = renderArea();
    const GLfloat l = a.x();
    const GLfloat t = a.y();
    const GLfloat w = a.width();
    const GLfloat h = a.height();

    m_verts = { l,     t + h,      // bottom-left
                l + w, t + h,      // bottom-right
                l + w, t,          // top-right
                l,     t };        // top-left
}

QRectF HwVideoSink::renderArea()
{
    if (!m_renderArea.isNull())
        return m_renderArea;

    QSizeF rs = m_videoSize;
    rs.scale(m_size, Qt::KeepAspectRatio);
    const qreal lm = (m_size.width()  - rs.width())  / 2.0;
    const qreal tm = (m_size.height() - rs.height()) / 2.0;
    m_renderArea = QRectF(QPointF(lm, tm), rs);
    return m_renderArea;
}

void HwVideoSink::resize(const QSizeF &size)
{
    if (size == m_size)
        return;
    m_size = size;
    m_renderArea = QRectF();
    m_needGeom = true;
}

void HwVideoSink::setVideoSize(const QSizeF &size)
{
    if (size == m_videoSize)
        return;
    m_videoSize = size;
    m_renderArea = QRectF();
    m_needGeom = true;
    HWLOG << "[youfish/hw] video size" << size;
    emit updateRequested();
}

bool HwVideoSink::hasFrame()
{
    QMutexLocker lock(&m_mutex);
    return m_queued || m_current;
}

void HwVideoSink::show_frame(GstElement *, GstBuffer *buffer, HwVideoSink *self)
{
    QMutexLocker lock(&self->m_mutex);
    GstBuffer *old = self->m_queued;
    self->m_queued = buffer ? gst_buffer_ref(buffer) : nullptr;
    self->m_bufferChanged = true;
    lock.unlock();

    if (old)
        gst_buffer_unref(old);
    QMetaObject::invokeMethod(self, "updateRequested", Qt::QueuedConnection);
}

void HwVideoSink::buffers_invalidated(GstElement *, HwVideoSink *self)
{
    {
        QMutexLocker lock(&self->m_mutex);
        self->m_invalidated = true;
    }
    QMetaObject::invokeMethod(self, "updateRequested", Qt::QueuedConnection);
}

void HwVideoSink::sink_caps_changed(GObject *obj, GParamSpec *, HwVideoSink *self)
{
    if (!obj || !GST_IS_PAD(obj))
        return;
    GstCaps *caps = gst_pad_get_current_caps(GST_PAD(obj));
    if (!caps)
        return;

    GstVideoInfo info;
    if (gst_caps_get_size(caps) >= 1 && gst_video_info_from_caps(&info, caps)) {
        QMetaObject::invokeMethod(self, "setVideoSize", Qt::QueuedConnection,
                                  Q_ARG(QSizeF, QSizeF(info.width, info.height)));
    }
    gst_caps_unref(caps);
}

void HwVideoSink::sink_notify(HwVideoSink *self, GObject *, gboolean isLastRef)
{
    if (isLastRef)
        self->cleanup();
}

void HwVideoSink::cleanup()
{
    if (!m_sink)
        return;

    destroyCachedTextures();

    if (m_showFrameId)   { g_signal_handler_disconnect(m_sink, m_showFrameId);   m_showFrameId = 0; }
    if (m_invalidatedId) { g_signal_handler_disconnect(m_sink, m_invalidatedId); m_invalidatedId = 0; }
    if (m_sinkPad) {
        if (m_notifyId) { g_signal_handler_disconnect(m_sinkPad, m_notifyId); m_notifyId = 0; }
        gst_object_unref(m_sinkPad);
        m_sinkPad = nullptr;
    }

    g_object_remove_toggle_ref(G_OBJECT(m_sink), (GToggleNotify)sink_notify, this);
    m_sink = nullptr;
}

void HwVideoSink::destroyCachedTextures()
{
    static const PFNEGLDESTROYIMAGEKHRPROC eglDestroyImageKHR =
        reinterpret_cast<PFNEGLDESTROYIMAGEKHRPROC>(eglGetProcAddress("eglDestroyImageKHR"));

    // GL deletes need a current context. Render-thread paths (buffers-invalidated) have one;
    // GUI-thread teardown (via the sink toggle-ref) doesn't, so there we defer the texture-name
    // delete to the next render-thread paint (drainOrphanTextures) rather than leak it. The
    // EGLImage and the pinned GstMemory are released immediately either way (EGL/gst calls are
    // thread-agnostic), so no droid buffer stays referenced past teardown.
    const bool haveGl = QOpenGLContext::currentContext() != nullptr;
    for (CachedTexture &t : m_textures) {
        if (haveGl) {
            glDeleteTextures(1, &t.textureId);
        } else {
            QMutexLocker lock(&s_orphanMutex);
            s_orphanTextures.push_back(t.textureId);
        }
        if (eglDestroyImageKHR && m_dpy != EGL_NO_DISPLAY)
            eglDestroyImageKHR(m_dpy, t.image);
        gst_memory_unref(t.memory);
    }
    m_textures.clear();
}
