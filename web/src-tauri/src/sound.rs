use serde::Deserialize;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) enum SystemSoundCue {
    Attention,
    TurnEnd,
}

#[tauri::command]
pub(crate) fn play_system_sound(cue: SystemSoundCue) -> bool {
    #[cfg(target_os = "macos")]
    {
        use objc2_app_kit::{NSSound, NSSoundName};

        let sound_name = match cue {
            SystemSoundCue::Attention => "Glass",
            SystemSoundCue::TurnEnd => "Pop",
        };
        let sound_name = NSSoundName::from_str(sound_name);
        return NSSound::soundNamed(&sound_name).is_some_and(|sound| sound.play());
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = cue;
        false
    }
}
