# Azur Promilia English Fan Patch

> [!WARNING]
> Use at your own risk. We are not responsible for any action taken against your account as a result of using this translation tool.

An unofficial English translation of the in-game text for **Azur Promilia (Japanese PC version)**: story dialogue, choices, quests, items, menus and settings. Voice audio, images and the launcher are not changed.

This project is not affiliated with or endorsed by the developers or publishers of Azur Promilia.

## Screenshots

| Graphics | Audio | Account |
|:---:|:---:|:---:|
| <a href="docs/screenshots/graphics.jpg"><img src="docs/screenshots/graphics.jpg" width="280" alt="Graphics settings in English"></a> | <a href="docs/screenshots/audio.jpg"><img src="docs/screenshots/audio.jpg" width="280" alt="Audio settings in English"></a> | <a href="docs/screenshots/account.jpg"><img src="docs/screenshots/account.jpg" width="280" alt="Account settings in English"></a> |

## Requirements

- Windows PC version of Azur Promilia (Japanese client)
- The game version the patch was made for. Each release lists it, and the patcher checks it before changing anything.

## Install

1. Download `AP-English-Patcher-vX.Y.zip` from [Releases](../../releases) and extract it.
2. Close the game.
3. Run `AP-English-Patcher.exe`. It finds the game folder automatically, or click **Browse...** and select the folder that contains `AzurPromiliaJP.exe`.
4. Click **Install English Patch**.

To go back to the original Japanese text, run the patcher again and click **Restore Japanese**.

## When the game updates

A game update replaces the patched files with new Japanese ones, and the patcher will report a different game version. Wait for a new release that supports the new version.

The patcher checks [Releases](../../releases) when it starts and shows an **update** link when a newer patch is out. Clicking it (after you confirm) downloads only the new translation data, a few MB, from this repository's releases, checks it against its published SHA-256, and reinstalls the English patch if it was installed. It never downloads or runs a program, and nothing is downloaded until you click. To turn the check off, add `"check_updates": false` to `%LOCALAPPDATA%\AP-English-Patcher\settings.json`.

If the patcher ever reports unrecognized files, use the in-game resource check under **Settings → Other → Resource Integrity** to restore the original files.

## Alternatives

These are separate community projects, not affiliated with this patch.

- **Recommended:** [Azur Promilia English by Lani27](https://github.com/Lani27/azur-promilia-english-patch): another English patcher for the Japanese PC client, with its own translation, interface layout fixes and English news articles. Use one patch or the other, not both: restore the Japanese text with one patcher before applying the other.

If you would rather not change any game files, these projects translate what is on screen with an overlay instead:

- [Game Translation HUD](https://github.com/ph1-st/game-translation-hud): reads the game window and draws the translation over the original text. It has a built-in Azur Promilia preset.
- [DanteTL](https://github.com/nhmelo/danteTL): a real-time translation overlay with offline or cloud translation models.

## How it works

The patcher never ships game files. It rebuilds the two text archives on your PC from your own installed files, adding the English text, and checks every result against the exact version that was tested before installing it. Restoring reverses the same changes exactly, so no backup is needed.
