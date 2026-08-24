#ifndef HWVIDEOSINK_H
#define HWVIDEOSINK_H

#include <QObject>
#include <QMutex>
#include <QMatrix4x4>
#include <QSizeF>
#include <QRectF>
#include <QVector>
#include <QPointer>
#include <QOpenGLContext>
#include <vector>

#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <gst/gst.h>

class QGLShaderProgram;

// Zero-copy hardware video renderer for SailfishOS / libhybris.
//
// gst-droid's droidvdec decodes into Android graphic buffers; droideglsink hands each
// decoded buffer to us as a native EGLImage. We bind that image to a GL_TEXTURE_EXTERNAL_OES
// texture and draw it straight into the owning QQuickPaintedItem's FBO — no CPU copy, no
// colour convert. This is the hardware equivalent of the software appsink->QImage path.
//
// Adapted from gst-droid's RendererNemo (Mohammed Sameer, LGPL), trimmed to flat video and
// with its known bugs fixed: GL_LINEAR filters (external-OES has no mipmaps), a single
// fragment shader (the original added it twice), and GL teardown guarded against running
// without a current context.
//
// Threading: show_frame / buffers_invalidated / sink_caps_changed run on the GStreamer
// streaming thread and touch shared state ONLY under m_mutex. All GL/EGL work happens in
// paint(), which the owner MUST call on the scene-graph render thread with a current context
// (inside begin/endNativePainting of a FramebufferObject-target QQuickPaintedItem).
class HwVideoSink : public QObject
{
    Q_OBJECT
public:
    explicit HwVideoSink(QObject *parent = nullptr);
    ~HwVideoSink() override;

    // Create the droideglsink once and return it to splice into the pipeline. The caller adds
    // it to the bin (which takes ownership); a toggle-ref keeps us notified of its teardown.
    // Returns null if the element can't be created (caller should fall back to software).
    GstElement *sinkElement();

    // Draw the latest decoded frame. matrix = QMatrix4x4(painter->combinedTransform()),
    // viewport = painter->viewport(). Returns false (draws nothing) if there's no frame yet
    // or GL isn't usable. Must run on the render thread with a current GL context.
    bool paint(const QMatrix4x4 &matrix, const QRectF &viewport);

    // Item size in device pixels; drives letterboxing. Cheap to call every paint.
    void resize(const QSizeF &size);

    // Cheap check for the owner's paint() to skip native painting when there's nothing yet.
    bool hasFrame();

signals:
    void updateRequested();            // frame ready / invalidated -> owner should update()

private slots:
    void setVideoSize(const QSizeF &size);

private:
    static void show_frame(GstElement *sink, GstBuffer *buffer, HwVideoSink *self);
    static void buffers_invalidated(GstElement *sink, HwVideoSink *self);
    static void sink_notify(HwVideoSink *self, GObject *object, gboolean isLastRef);
    static void sink_caps_changed(GObject *pad, GParamSpec *pspec, HwVideoSink *self);

    bool ensureGl();
    void ensureProgram();
    void handleContextChange();        // drop resources bound to a now-destroyed GL context
    void paintFrame(const QMatrix4x4 &matrix);
    void recalcGeometry();
    QRectF renderArea();
    void destroyCachedTextures();
    void cleanup();

    struct CachedTexture { GstMemory *memory; EGLImageKHR image; GLuint textureId; };

    GstElement *m_sink = nullptr;
    GstPad *m_sinkPad = nullptr;
    GstBuffer *m_queued = nullptr;
    GstBuffer *m_current = nullptr;
    QMutex m_mutex;                    // guards m_queued/m_current/flags across the thread hop
    gulong m_showFrameId = 0;
    gulong m_invalidatedId = 0;
    gulong m_notifyId = 0;
    bool m_bufferChanged = false;
    bool m_invalidated = false;
    bool m_displaySet = false;
    bool m_needGeom = true;

    QGLShaderProgram *m_program = nullptr;
    QPointer<QOpenGLContext> m_glCtx;  // context m_program/m_textures live in; auto-nulls on destroy
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC m_glEGLImageTargetTexture2DOES = nullptr;
    EGLDisplay m_dpy = EGL_NO_DISPLAY;

    QMatrix4x4 m_ortho;
    std::vector<GLfloat> m_verts;      // 8 floats: letterboxed quad in item-pixel space
    std::vector<GLfloat> m_texCoords;  // 8 floats
    QVector<CachedTexture> m_textures; // one EGLImage/texture per native buffer in the pool
    QSizeF m_size;                     // item size
    QSizeF m_videoSize;                // decoded frame size
    QRectF m_renderArea;               // cached letterbox rect
};

#endif // HWVIDEOSINK_H
