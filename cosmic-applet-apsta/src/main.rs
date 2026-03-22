// cosmic-applet-apsta/src/main.rs
//
// Entry point. COSMIC applets use cosmic::applet::run instead of
// cosmic::app::run — this registers the process with the COSMIC panel
// compositor and sets up the transparent headerless window geometry.

mod app;

fn main() -> cosmic::iced::Result {
    cosmic::applet::run::<app::ApstaApplet>(())
}
