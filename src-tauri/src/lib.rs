//! Desktop shell for Agent Studio: a single window that loads the hosted
//! Studio site. No frontend is bundled; the WebView keeps its own persistent
//! cookie jar, so the Django session survives relaunches.

use std::fs;

use serde::Deserialize;
use tauri::webview::NewWindowResponse;
use tauri::{AppHandle, Manager, WebviewUrl, WebviewWindowBuilder};
use url::Url;

pub const DEFAULT_URL: &str = "https://studio.makemoredigital.com/studio/";

// Optional override, e.g. to point a build at a local runserver:
//   ~/Library/Application Support/com.makemoredigital.studio/studio-desktop.json
//   {"url": "http://127.0.0.1:8000/studio/"}
const CONFIG_FILE: &str = "studio-desktop.json";

#[derive(Deserialize, Default)]
struct Config {
    url: Option<String>,
}

pub fn parse_config(text: &str) -> Option<Url> {
    serde_json::from_str::<Config>(text)
        .ok()?
        .url
        .and_then(|value| Url::parse(&value).ok())
        .filter(|url| matches!(url.scheme(), "http" | "https"))
}

fn studio_url(app: &AppHandle) -> Url {
    app.path()
        .app_config_dir()
        .ok()
        .and_then(|dir| fs::read_to_string(dir.join(CONFIG_FILE)).ok())
        .and_then(|text| parse_config(&text))
        .unwrap_or_else(|| Url::parse(DEFAULT_URL).expect("valid default url"))
}

/// Navigations that stay inside the shell. Everything else is handed to the
/// system browser so external links never trap the user in the WebView.
pub fn stays_in_shell(url: &Url, home: &Url) -> bool {
    match url.scheme() {
        "about" | "blob" | "data" => true,
        "http" | "https" => {
            url.scheme() == home.scheme()
                && url.host_str() == home.host_str()
                && url.port_or_known_default() == home.port_or_known_default()
        }
        _ => false,
    }
}

fn open_externally(url: &Url) {
    if let Err(error) = tauri_plugin_opener::open_url(url.as_str(), None::<&str>) {
        eprintln!("studio-desktop: could not open {url} externally: {error}");
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_window_state::Builder::new().build())
        .setup(|app| {
            let home = studio_url(app.handle());
            let nav_home = home.clone();
            let popup_home = home.clone();
            let popup_app = app.handle().clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(home))
                .title("Agent Studio")
                .inner_size(1280.0, 860.0)
                .min_inner_size(900.0, 600.0)
                .center()
                .zoom_hotkeys_enabled(true)
                .on_navigation(move |url| {
                    if stays_in_shell(url, &nav_home) {
                        return true;
                    }
                    open_externally(url);
                    false
                })
                // target=_blank: same-site links load in the one window,
                // anything else goes to the system browser.
                .on_new_window(move |url, _features| {
                    if stays_in_shell(&url, &popup_home) {
                        if let Some(window) = popup_app.get_webview_window("main") {
                            let _ = window.navigate(url);
                        }
                    } else {
                        open_externally(&url);
                    }
                    NewWindowResponse::Deny
                })
                .build()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Agent Studio");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn home() -> Url {
        Url::parse(DEFAULT_URL).unwrap()
    }

    #[test]
    fn same_site_navigation_stays_in_shell() {
        let home = home();
        assert!(stays_in_shell(
            &Url::parse("https://studio.makemoredigital.com/accounts/login/?next=/studio/")
                .unwrap(),
            &home
        ));
        assert!(stays_in_shell(
            &Url::parse("https://studio.makemoredigital.com:443/studio/agents/").unwrap(),
            &home
        ));
        assert!(stays_in_shell(&Url::parse("about:blank").unwrap(), &home));
    }

    #[test]
    fn foreign_navigation_leaves_the_shell() {
        let home = home();
        assert!(!stays_in_shell(
            &Url::parse("https://docs.example.com/").unwrap(),
            &home
        ));
        assert!(!stays_in_shell(
            &Url::parse("http://studio.makemoredigital.com/studio/").unwrap(),
            &home
        ));
        assert!(!stays_in_shell(
            &Url::parse("mailto:someone@example.com").unwrap(),
            &home
        ));
    }

    #[test]
    fn config_override_requires_an_http_url() {
        assert_eq!(
            parse_config(r#"{"url": "http://127.0.0.1:8000/studio/"}"#)
                .unwrap()
                .as_str(),
            "http://127.0.0.1:8000/studio/"
        );
        assert!(parse_config(r#"{"url": "file:///etc/passwd"}"#).is_none());
        assert!(parse_config(r#"{"url": "not a url"}"#).is_none());
        assert!(parse_config("{}").is_none());
        assert!(parse_config("nonsense").is_none());
    }
}
