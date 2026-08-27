Name:       harbour-fintube
Summary:    A YouTube client for Sailfish OS
Version:    1.1.0
Release:    1
License:    GPLv3
URL:        https://github.com/sandstorm244/harbour-fintube
Source0:    %{name}-%{version}.tar.bz2

Requires:   sailfishsilica-qt5 >= 0.10.9
Requires:   pyotherside-qml-plugin-python3-qt5
Requires:   qt5-qtdeclarative-import-multimedia
# NOTE: yt-dlp is intentionally NOT a dependency. The app checks for it at launch
# and the user installs / updates it themselves (see README).

BuildRequires:  pkgconfig(sailfishapp) >= 1.0.2
BuildRequires:  pkgconfig(Qt5Core)
BuildRequires:  pkgconfig(Qt5Qml)
BuildRequires:  pkgconfig(Qt5Quick)
BuildRequires:  pkgconfig(Qt5OpenGL)
BuildRequires:  pkgconfig(gstreamer-1.0)
BuildRequires:  pkgconfig(gstreamer-app-1.0)
BuildRequires:  pkgconfig(gstreamer-video-1.0)
# Hardware-decode path (droideglsink -> EGLImage -> external-OES texture); only used at
# runtime when YOUFISH_HWDEC=1, but always compiled/linked in. GLESv2 comes via Qt5OpenGL.
BuildRequires:  pkgconfig(egl)
BuildRequires:  pkgconfig(nemo-gstreamer-interfaces-1.0)
BuildRequires:  desktop-file-utils

%description
FinTube is a native Sailfish OS client for YouTube. It uses an external yt-dlp
binary (managed and updated by the user) to resolve video streams, and plays them
with the device's native media stack.

%prep
%setup -q -n %{name}-%{version}

%build
%qmake5
make %{?_smp_mflags}

%install
%qmake5_install

%files
%defattr(-,root,root,-)
%{_bindir}/%{name}
%{_datadir}/%{name}
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/86x86/apps/%{name}.png
%{_datadir}/icons/hicolor/108x108/apps/%{name}.png
%{_datadir}/icons/hicolor/128x128/apps/%{name}.png
%{_datadir}/icons/hicolor/172x172/apps/%{name}.png
%{_datadir}/dbus-1/services/harbour.fintube.service
