// cosmic-applet-apsta/src/app.rs
//
// Full Application trait implementation for the apsta COSMIC panel applet.
//
// Architecture:
//   State (ApstaApplet) — holds hotspot status, config, and popup window ID
//   Message             — all events: user interactions + async command results
//   update()            — pure state transitions; spawns async Commands for
//                         shell operations so the UI never blocks
//   view()              — panel icon (shows hotspot on/off at a glance)
//   view_window()       — popup content: toggle, SSID/password, status, detect

use cosmic::{
    app::{Core, Task},
    iced::{
        platform_specific::shell::commands::popup::{destroy_popup, get_popup},
        window,
        Limits,
        Length,
        Alignment,
    },
    widget,
    Element,
};
use std::process::Stdio;
use tokio::process::Command as TokioCommand;

// ── App ID ────────────────────────────────────────────────────────────────────

// Must match the desktop entry filename (without .desktop)
pub const APP_ID: &str = "com.github.apsta.Applet";

// ── State ─────────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub struct HotspotStatus {
    pub active:    bool,
    pub ssid:      String,
    pub interface: String,   // e.g. "wlo1_ap"
    pub channel:   String,   // e.g. "6"
    pub band:      String,   // e.g. "2.4 GHz"
}

impl Default for HotspotStatus {
    fn default() -> Self {
        Self {
            active:    false,
            ssid:      String::new(),
            interface: String::new(),
            channel:   String::new(),
            band:      String::new(),
        }
    }
}

pub struct ApstaApplet {
    core:         Core,
    popup:        Option<window::Id>,
    status:       HotspotStatus,
    // Editable config fields in the popup
    ssid_input:   String,
    pass_input:   String,
    // Output from `apsta detect` shown in the popup
    detect_output: String,
    // True while an async command is running (shows spinner)
    loading:      bool,
    // Error message to display
    error:        Option<String>,
    ssid_edited:  bool, // tracks whether user has edited SSID input (for pre-fill logic)
}

// ── Messages ──────────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub enum Message {
    // Panel icon pressed — toggle popup
    TogglePopup,
    // User edits SSID / password fields
    SsidChanged(String),
    PassChanged(String),
    // Buttons
    StartHotspot,
    StopHotspot,
    RunDetect,
    // Background polling tick — keeps panel icon in sync with daemon state
    RefreshStatus,
    // Async command results
    HotspotStarted(Result<(), String>),
    HotspotStopped(Result<(), String>),
    StatusRefreshed(Result<HotspotStatus, String>),
    DetectFinished(Result<String, String>),
}

// ── Application trait ─────────────────────────────────────────────────────────

impl cosmic::Application for ApstaApplet {
    type Executor = cosmic::executor::Default;
    type Flags    = ();
    type Message  = Message;

    const APP_ID: &'static str = APP_ID;

    fn core(&self) -> &Core { &self.core }
    fn core_mut(&mut self) -> &mut Core { &mut self.core }

    fn init(core: Core, _flags: Self::Flags) -> (Self, Task<Self::Message>) {
        let applet = Self {
            core,
            popup:         None,
            status:        HotspotStatus::default(),
            ssid_input:    String::from("apsta-hotspot"),
            pass_input:    String::from("changeme123"),
            detect_output: String::new(),
            loading:       false,
            error:         None,
            ssid_edited:   false,
        };
        // Refresh status on startup
        let cmd = Task::perform(async_get_status(), |result| Message::StatusRefreshed(result).into());
        (applet, cmd)
    }

    // ── Panel icon view ───────────────────────────────────────────────────────

    fn view(&self) -> Element<'_, Message> {
        // Choose icon based on hotspot state
        let icon_name = if self.status.active {
            "network-wireless-hotspot-symbolic"
        } else {
            "network-wireless-symbolic"
        };

        self.core
            .applet
            .icon_button(icon_name)
            .on_press_down(Message::TogglePopup)
            .into()
    }

    // ── Popup view ────────────────────────────────────────────────────────────

    fn view_window(&self, _id: window::Id) -> Element<'_, Message> {
        // Header
        let title = widget::text("apsta — Hotspot Manager")
            .size(16);

        // Status row
        let status_icon = if self.status.active { "✔" } else { "✘" };
        let status_label = if self.status.active {
            format!(
                "{} Active — {} on {} ch{} ({})",
                status_icon,
                self.status.ssid,
                self.status.interface,
                self.status.channel,
                self.status.band,
            )
        } else {
            format!("{} Hotspot is off", status_icon)
        };
        let status_row = widget::text(status_label).size(13);

        // SSID input
        let ssid_row = widget::row::with_children(vec![
            widget::text("SSID").size(13).into(),
            widget::text_input("apsta-hotspot", &self.ssid_input)
                .on_input(Message::SsidChanged)
                .into(),
        ])
        .spacing(8)
        .align_y(Alignment::Center);

        // Password input
        let pass_row = widget::row::with_children(vec![
            widget::text("Password").size(13).into(),
            widget::text_input("password", &self.pass_input)
                .password()
                .on_input(Message::PassChanged)
                .into(),
        ])
        .spacing(8)
        .align_y(Alignment::Center);

        // Toggle button
        let toggle_btn: Element<Message> = if self.loading {
            widget::text("Working…").size(13).into()
        } else if self.status.active {
            widget::button::destructive("Stop Hotspot")
                .on_press(Message::StopHotspot)
                .into()
        } else {
            widget::button::suggested("Start Hotspot")
                .on_press(Message::StartHotspot)
                .into()
        };

        // Detect button
        let detect_btn = widget::button::standard("Run Detect")
            .on_press(Message::RunDetect);

        // Error / detect output
        let info_text: Element<Message> = if let Some(ref e) = self.error {
            widget::text(format!("Error: {}", e))
                .size(11)
                .into()
        } else if !self.detect_output.is_empty() {
            widget::text(&self.detect_output).size(11).into()
        } else {
            widget::text("").into()
        };

        // Compose the popup column
        let content = widget::column::with_children(vec![
            title.into(),
            widget::divider::horizontal::default().into(),
            status_row.into(),
            widget::divider::horizontal::light().into(),
            ssid_row.into(),
            pass_row.into(),
            toggle_btn,
            detect_btn.into(),
            info_text,
        ])
        .spacing(10)
        .padding(16)
        .width(Length::Fill);

        self.core.applet.popup_container(content).into()
    }

    // ── Update ────────────────────────────────────────────────────────────────

    fn update(&mut self, message: Message) -> Task<Self::Message> {
        match message {
            Message::TogglePopup => {
                if let Some(popup_id) = self.popup.take() {
                    // Close existing popup
                    return destroy_popup(popup_id);
                }

                // Open popup
                let new_id = window::Id::unique();
                self.popup = Some(new_id);

                let mut popup_settings = self.core.applet.get_popup_settings(
                    self.core.main_window_id().unwrap(),
                    new_id,
                    None,
                    None,
                    None,
                );
                popup_settings.positioner.size_limits = Limits::NONE
                    .min_width(280.0)
                    .max_width(380.0)
                    .min_height(200.0)
                    .max_height(480.0);

                // Refresh status every time the popup opens
                let refresh = Task::perform(async_get_status(), |result| {
                    Message::StatusRefreshed(result).into()
                });
                return Task::batch(vec![
                    get_popup(popup_settings),
                    refresh,
                ]);
            }

            Message::SsidChanged(s) => {
                self.ssid_input = s;
                self.ssid_edited = !self.ssid_input.trim().is_empty();
            }

            Message::PassChanged(s) => {
                self.pass_input = s;
            }

            Message::StartHotspot => {
                self.loading = true;
                self.error   = None;
                let ssid = self.ssid_input.clone();
                let pass = self.pass_input.clone();
                return Task::perform(async_start_hotspot(ssid, pass), |result| {
                    Message::HotspotStarted(result).into()
                });
            }

            Message::StopHotspot => {
                self.loading = true;
                self.error   = None;
                return Task::perform(async_stop_hotspot(), |result| {
                    Message::HotspotStopped(result).into()
                });
            }

            Message::RunDetect => {
                self.detect_output = String::from("Running detect…");
                self.error         = None;
                return Task::perform(async_run_detect(), |result| {
                    Message::DetectFinished(result).into()
                });
            }

            // Silent background poll — does not set loading or clear errors
            // so it doesn't interfere with in-progress user actions.
            Message::RefreshStatus => {
                return Task::perform(async_get_status(), |result| {
                    Message::StatusRefreshed(result).into()
                });
            }

            // ── Async results ─────────────────────────────────────────────

            Message::HotspotStarted(result) => {
                self.loading = false;
                match result {
                    Ok(()) => {
                        // Refresh status to get updated interface/channel info
                        return Task::perform(async_get_status(), |result| {
                            Message::StatusRefreshed(result).into()
                        });
                    }
                    Err(e) => self.error = Some(e),
                }
            }

            Message::HotspotStopped(result) => {
                self.loading = false;
                match result {
                    Ok(()) => {
                        self.status = HotspotStatus::default();
                    }
                    Err(e) => self.error = Some(e),
                }
            }

            Message::StatusRefreshed(result) => {
                match result {
                    Ok(status) => {
                        // Pre-fill inputs from live config if user hasn't typed
                        if !self.ssid_edited && !status.ssid.is_empty() {
                            self.ssid_input = status.ssid.clone();
                        }
                        self.status = status;
                    }
                    Err(_) => {
                        // Status read failure is non-fatal — keep previous state
                    }
                }
            }

            Message::DetectFinished(result) => {
                match result {
                    Ok(output) => self.detect_output = output,
                    Err(e)     => self.error = Some(e),
                }
            }
        }

        Task::none()
    }

    fn style(&self) -> Option<cosmic::iced::theme::Style> {
        Some(cosmic::applet::style())
    }

    /// Poll hotspot status in adaptive intervals so the panel icon stays in
    /// sync without constant wakeups while inactive.
    fn subscription(&self) -> cosmic::iced::Subscription<Message> {
        let poll_secs = if self.status.active || self.popup.is_some() { 5 } else { 30 };
        cosmic::iced::time::every(std::time::Duration::from_secs(poll_secs))
            .map(|_| Message::RefreshStatus)
    }
}

// ── Async shell helpers ───────────────────────────────────────────────────────
//
// All shell operations are async so the UI thread never blocks.
// Each function spawns `apsta` as a subprocess and parses its output.

const APSTA: &str = "/usr/local/bin/apsta";

/// Read /etc/apsta/config.json to get current hotspot state.
/// We parse JSON directly rather than parsing apsta's coloured terminal output.
async fn async_get_status() -> Result<HotspotStatus, String> {
    let config_path = "/etc/apsta/config.json";
    let content = tokio::fs::read_to_string(config_path)
        .await
        .map_err(|e| format!("Could not read config: {}", e))?;

    let v: serde_json::Value = serde_json::from_str(&content)
        .map_err(|e| format!("Config parse error: {}", e))?;

    let ap_interface = v["ap_interface"].as_str().unwrap_or("").to_string();
    let active = !ap_interface.is_empty();

    // Get channel info from `iw dev <ap_iface> info` if active
    let (channel, band) = if active {
        get_channel_info(&ap_interface).await
    } else {
        (String::new(), String::new())
    };

    Ok(HotspotStatus {
        active,
        ssid:      v["ssid"].as_str().unwrap_or("").to_string(),
        interface: ap_interface,
        channel,
        band,
    })
}

async fn get_channel_info(iface: &str) -> (String, String) {
    let out = TokioCommand::new("iw")
        .args(["dev", iface, "info"])
        .output()
        .await;

    if let Ok(out) = out {
        let text = String::from_utf8_lossy(&out.stdout);
        // "channel 6 (2437 MHz), ..."
        if let Some(cap) = regex_channel(&text) {
            return cap;
        }
    }
    (String::new(), String::new())
}

fn regex_channel(text: &str) -> Option<(String, String)> {
    // Simple line scan — no regex crate dependency
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with("channel ") {
            // "channel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz"
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 3 {
                let ch = parts[1].to_string();
                // extract MHz from "(2437"
                let freq_str = parts[2].trim_start_matches('(');
                let freq: u32 = freq_str.parse().unwrap_or(0);
                let band = if freq >= 5000 {
                    "5 GHz".to_string()
                } else {
                    "2.4 GHz".to_string()
                };
                return Some((ch, band));
            }
        }
    }
    None
}

/// Run `sudo apsta start` with updated SSID/password written to config first.
///
/// All three operations are batched into a single pkexec invocation.
/// Without this, every pkexec call triggers a separate Polkit auth dialog —
/// the user would have to type their password three times in a row.
///
/// Arguments are passed as positional parameters ($1, $2) to the sh -c script
/// rather than interpolated into the script string. This prevents shell
/// injection if the user types a SSID like: foo"; rm -rf /
async fn async_start_hotspot(ssid: String, pass: String) -> Result<(), String> {
    let script = format!(
        "{apsta} config --set ssid=\"$1\" && \
         {apsta} config --set password=\"$2\" && \
         {apsta} start",
        apsta = APSTA
    );
    let out = TokioCommand::new("pkexec")
        .args(["sh", "-c", &script, "--", &ssid, &pass])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("pkexec failed: {}", e))?;

    pkexec_result(out)
}

async fn async_stop_hotspot() -> Result<(), String> {
    let out = TokioCommand::new("pkexec")
        .args([APSTA, "stop"])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("pkexec failed: {}", e))?;
    pkexec_result(out)
}

async fn async_run_detect() -> Result<String, String> {
    // Run without sudo — detect is read-only
    let out = TokioCommand::new(APSTA)
        .arg("detect")
        // Strip ANSI colour codes by setting NO_COLOR
        .env("NO_COLOR", "1")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("Failed to run apsta: {}", e))?;

    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();

    if out.status.success() {
        // Trim to the Verdict section for display in the small popup
        Ok(trim_to_verdict(&stdout))
    } else {
        Err(if stderr.is_empty() { stdout } else { stderr })
    }
}

/// Extract just the Verdict section from `apsta detect` output for compact display.
fn trim_to_verdict(output: &str) -> String {
    let mut in_verdict = false;
    let mut lines: Vec<String> = Vec::new();
    for line in output.lines() {
        if line.contains("Verdict") {
            in_verdict = true;
        }
        if in_verdict {
            // Strip ANSI escapes (simple approach — remove ESC sequences)
            let clean = strip_ansi(line);
            if !clean.trim().is_empty() {
                lines.push(clean);
            }
        }
    }
    if lines.is_empty() {
        output.lines().take(6).collect::<Vec<_>>().join("\n")
    } else {
        lines.join("\n")
    }
}

fn strip_ansi(s: &str) -> String {
    // Remove ANSI escape sequences (ESC [ ... m) without a crate dependency.
    // We require the exact ESC[ prefix before consuming characters — a bare ESC
    // followed by text containing 'm' would otherwise eat valid output.
    let mut result = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == '\x1b' {
            // Only consume as an ANSI sequence if the next char is '['
            if chars.peek() == Some(&'[') {
                chars.next(); // consume '['
                for c2 in chars.by_ref() {
                    if c2 == 'm' { break; }
                }
            }
            // If next char is not '[', it's a bare ESC — skip just the ESC,
            // leave the following characters intact.
        } else {
            result.push(c);
        }
    }
    result
}

/// Run an apsta subcommand with pkexec for privilege escalation.
/// pkexec shows the system authentication dialog — no terminal sudo needed.
#[allow(dead_code)]
async fn run_apsta_sudo(args: &[&str]) -> Result<(), String> {
    let mut cmd_args = vec![APSTA];
    cmd_args.extend_from_slice(args);

    let out = TokioCommand::new("pkexec")
        .args(&cmd_args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .await
        .map_err(|e| format!("pkexec failed: {}", e))?;

    pkexec_result(out)
}

/// Interpret a pkexec process output into Ok(()) or a clean Err string.
///
/// pkexec exit codes:
///   0   — success
///   126 — authentication dialog was cancelled or dismissed by the user
///   127 — command not found
///   other — command failed
fn pkexec_result(out: std::process::Output) -> Result<(), String> {
    if out.status.success() {
        return Ok(());
    }
    // Exit code 126 = Polkit dialog cancelled — present a clean message
    // instead of the raw "Error executing command as another user: Request
    // dismissed" string that pkexec writes to stderr.
    if out.status.code() == Some(126) {
        return Err("Authentication cancelled.".to_string());
    }
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    let stdout = String::from_utf8_lossy(&out.stdout).to_string();
    Err(if !stderr.is_empty() { stderr } else { stdout })
}
