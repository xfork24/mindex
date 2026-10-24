# Requirements Document

## Introduction

The Settings page currently labels the voice configuration section "TTS 设置" (zh) / "TTS settings" (en). This feature renames that section to "语音合成设置" (zh) / "Speech synthesis settings" (en) and unifies other generic user-visible uses of the abbreviation "TTS" to "语音合成" (zh) / "speech synthesis" (en). Product and model names that contain "TTS" (Qwen3-TTS, MiniMax TTS, DashScope TTS) remain unchanged. Affected areas include the Settings page HTML, the shared message catalog (messages.json and the generated messages.js), the Settings and Studio frontend scripts, documentation, backend speech model text, and the related tests.

## Glossary

- **Settings_Page**: The frontend page defined by frontend/src/settings/settings.html and its scripts.
- **Message_Catalog**: The i18n catalog in frontend/src/shared/messages.json and the generated frontend/src/shared/messages.js.
- **Build_Script**: The script scripts/build-messages.py that regenerates the Message_Catalog outputs.
- **Generic_TTS_Wording**: User-visible text that uses "TTS" as a generic term for the speech synthesis capability, such as "TTS 设置", "TTS 服务", or "TTS service".
- **Protected_Product_Name**: One of the strings "Qwen3-TTS", "MiniMax TTS", or "DashScope TTS".
- **Section_Key**: The i18n key ui.3401af34627a used for the Settings_Page voice section label and heading.
- **Docs**: The markdown files under the docs directory.
- **Backend_Speech_Model**: The file backend/src/models/speech.py.
- **Test_Suite**: The tests in tests/settings_releases_ui.cjs, test_studio_tts_feedback.py, test_cloud_adapters.py, and test_voices.py.

## Requirements

### Requirement 1: Rename the Settings voice section

**User Story:** As a Chinese-speaking user, I want the Settings page voice section titled "语音合成设置", so that I see a descriptive term instead of the abbreviation "TTS".

#### Acceptance Criteria

1. THE Message_Catalog SHALL map the Section_Key to the zh value "语音合成设置".
2. THE Message_Catalog SHALL map the Section_Key to the en value "Speech synthesis settings".
3. THE Settings_Page SHALL render the h2 heading of the voice section with the text "语音合成设置" in the source HTML default text.
4. THE Settings_Page SHALL set the aria-label attribute of the voice section to "语音合成设置" in the source HTML default text.
5. WHEN the interface language is en, THE Settings_Page SHALL display "Speech synthesis settings" as the voice section heading and aria-label.

### Requirement 2: Unify generic TTS wording

**User Story:** As a user, I want other generic mentions of TTS replaced by speech synthesis wording, so that terminology is consistent across the product.

#### Acceptance Criteria

1. THE Message_Catalog SHALL replace each Generic_TTS_Wording occurrence in zh values with the equivalent text using "语音合成".
2. THE Message_Catalog SHALL replace each Generic_TTS_Wording occurrence in en values with the equivalent text using "speech synthesis".
3. THE Message_Catalog SHALL update the messages containing "check the TTS service", "TTS 服务返回的 JSON", and "TTS service did not return recognizable audio data" to use the unified wording.
4. THE Message_Catalog SHALL update the Studio voice-load failure message that refers to "the TTS service in Settings" to use "speech synthesis service".
5. WHEN Generic_TTS_Wording appears in user-visible strings in settings.js, studio.js, or Backend_Speech_Model, THE respective file SHALL use "语音合成" (zh) or "speech synthesis" (en) instead.
6. WHEN Generic_TTS_Wording appears in user-facing text in Docs, THE Docs SHALL use "语音合成" (zh) or "speech synthesis" (en) instead.

### Requirement 3: Preserve product names

**User Story:** As a maintainer, I want product and model names preserved, so that references to specific providers remain accurate.

#### Acceptance Criteria

1. THE Message_Catalog SHALL retain every Protected_Product_Name unchanged in zh and en values.
2. THE Settings_Page, settings.js, studio.js, Docs, and Backend_Speech_Model SHALL retain every Protected_Product_Name unchanged.
3. WHEN a message contains both a Protected_Product_Name and Generic_TTS_Wording, THE Message_Catalog SHALL replace only the Generic_TTS_Wording.
4. THE Message_Catalog SHALL retain all message keys, placeholders (such as {p0}), and the existing key-to-source-text hashing scheme unchanged except for changes to display values.

### Requirement 4: Regenerate message artifacts

**User Story:** As a developer, I want the generated message artifacts consistent with the source catalog, so that the runtime shows the new wording.

#### Acceptance Criteria

1. WHEN messages.json is modified, THE Build_Script SHALL regenerate messages.js so that messages.js reflects the updated messages.json.
2. THE Settings_Page source HTML data-i18n attributes SHALL reference keys that exist in the Message_Catalog.
3. IF the Build_Script reports an error, THEN THE Build_Script SHALL exit with a non-zero status code.

### Requirement 5: Update tests

**User Story:** As a maintainer, I want the tests aligned with the new wording, so that the test suite passes and guards the rename.

#### Acceptance Criteria

1. THE Test_Suite SHALL assert "语音合成设置" where tests/settings_releases_ui.cjs line 123 previously asserted "TTS 设置".
2. WHEN test_studio_tts_feedback.py, test_cloud_adapters.py, or test_voices.py assert on Generic_TTS_Wording, THE Test_Suite SHALL assert on the unified wording instead.
3. WHEN the Test_Suite is executed after the rename, THE Test_Suite SHALL complete with all tests passing.
4. THE Test_Suite SHALL contain an assertion that Protected_Product_Names remain present in the corresponding user-visible strings.

### Requirement 6: Limit scope to user-visible text

**User Story:** As a maintainer, I want non-user-visible identifiers left intact, so that the rename does not break code or APIs.

#### Acceptance Criteria

1. THE rename SHALL leave code identifiers, file names, function names, API routes, configuration keys, and log-only strings containing "tts" unchanged.
2. WHEN a candidate occurrence of "TTS" is found in a file, THE implementer SHALL classify the occurrence as user-visible Generic_TTS_Wording, a Protected_Product_Name, or a non-user-visible identifier before editing.
3. WHEN the rename is complete, THE repository SHALL contain no remaining user-visible Generic_TTS_Wording in the Message_Catalog, Settings_Page, settings.js, studio.js, Docs, or Backend_Speech_Model outside Protected_Product_Names.
