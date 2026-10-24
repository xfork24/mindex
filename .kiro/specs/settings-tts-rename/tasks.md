# Implementation Plan: settings-tts-rename

## Overview

Rename the generic user-visible "TTS" wording to "语音合成" / "speech synthesis" (Settings voice section heading becomes "语音合成设置" / "Speech synthesis settings"), keeping Protected_Product_Names, identifiers, keys, routes and log-only strings unchanged. Implementation languages: Python (backend, tooling, tests) and JavaScript (frontend tests). Steps: classify occurrences, edit the catalog, regenerate messages.js, edit HTML/JS/Python/docs, then update and add tests.

## Tasks

- [ ] 1. Classify TTS occurrences and prepare the baseline
  - [ ] 1.1 Scan the scoped files and classify every "TTS" match
    - Search messages.json, settings.html, settings.js, studio.js, backend/src/models/speech.py and docs/*.md for case-insensitive "tts"
    - Classify each match as Generic_TTS_Wording, Protected_Product_Name, or non-user-visible identifier before editing
    - Keep the list of Generic_TTS_Wording locations as the edit worklist
    - _Requirements: 6.2, 6.1_

- [ ] 2. Update the message catalog and regenerate artifacts
  - [ ] 2.1 Edit display values in frontend/src/shared/messages.json
    - Set `ui.3401af34627a` zh to "语音合成设置" and en to "Speech synthesis settings"
    - Replace generic wording in the messages for "check the TTS service", "TTS 服务返回的 JSON", "TTS service did not return recognizable audio data", and the Studio voice-load failure ("the speech synthesis service in Settings")
    - Replace every other generic "TTS" in zh/en values using "语音合成" / "speech synthesis"
    - Keep keys, placeholders and Protected_Product_Names unchanged; when a message has both, replace only the generic wording
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 2.3, 2.4, 3.1, 3.3, 3.4_

  - [ ] 2.2 Regenerate frontend/src/shared/messages.js
    - Run `python scripts/build-messages.py` and confirm `python scripts/build-messages.py --check` exits 0
    - Do not edit messages.js by hand
    - _Requirements: 4.1_

- [ ] 3. Update HTML, frontend scripts, backend model and docs
  - [ ] 3.1 Update frontend/src/settings/settings.html
    - Set the voice section `aria-label` and `<h2>` default text to "语音合成设置"
    - Leave `data-i18n*` attributes unchanged
    - _Requirements: 1.3, 1.4, 1.5, 4.2_

  - [ ] 3.2 Update user-visible strings in settings.js and studio.js
    - Replace only user-visible Generic_TTS_Wording literals
    - Leave `ROLE_ORDER`, `ROLE_DESC.tts`, `PROVIDERS.tts`, `tts_backend` and other identifiers unchanged
    - Keep Protected_Product_Names unchanged
    - _Requirements: 2.5, 3.2, 6.1_

  - [ ] 3.3 Update user-visible text in backend/src/models/speech.py
    - Docstring: "TTS 语音合成客户端..." to "语音合成客户端..."
    - Change the two ModelError messages to "语音合成服务未返回可识别的音频数据" and "语音合成服务返回的 JSON 中没有可解码的音频字段"
    - Keep `config.studio.tts` and other identifiers unchanged
    - Ensure the matching catalog source texts from 2.1 use the same wording
    - _Requirements: 2.3, 2.5, 3.2, 6.1_

  - [ ] 3.4 Update user-facing prose in docs/*.md
    - Replace generic "TTS" in prose with "语音合成" / "speech synthesis"
    - Leave code fences, paths, config keys and Protected_Product_Names unchanged
    - _Requirements: 2.6, 3.2, 6.1_

- [ ] 4. Checkpoint - Verify edits and build
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Update existing tests
  - [ ] 5.1 Update tests/settings_releases_ui.cjs
    - Change the line 123 assertion from "TTS 设置" to "语音合成设置"
    - Add a `lang=en` case asserting the heading and aria-label are "Speech synthesis settings"
    - Add an assertion that Protected_Product_Names remain in the relevant user-visible strings
    - _Requirements: 1.5, 5.1, 5.4_

  - [ ] 5.2 Update test_studio_tts_feedback.py, test_cloud_adapters.py and test_voices.py
    - Switch assertions on Generic_TTS_Wording to the unified wording
    - Assert that Protected_Product_Names remain present in the corresponding strings
    - _Requirements: 5.2, 5.4, 3.2_

- [ ] 6. Add guard tests in test_tts_rename.py
  - [ ] 6.1 Create test_tts_rename.py with the masking/replacement helpers and example tests
    - Implement `mask_protected`, `unmask_protected`, `has_generic_tts`, `replace_generic` as in the design
    - Add example tests for the Section_Key values and the three named messages plus the Studio voice-load message
    - _Requirements: 1.1, 1.2, 2.3, 2.4, 5.2_

  - [ ]* 6.2 Write property test for no generic TTS wording in the catalog
    - **Property 1: No generic TTS wording remains in the catalog**
    - **Validates: Requirements 2.1, 2.2, 6.3**

  - [ ]* 6.3 Write test for no generic TTS wording in scoped files and docs
    - **Property 2: No generic TTS wording in scoped source files and docs**
    - **Validates: Requirements 2.5, 2.6, 6.3**

  - [ ]* 6.4 Write test for protected product names preserved
    - **Property 3: Protected product names are preserved**
    - Compare against baseline from `git show HEAD:frontend/src/shared/messages.json` and scoped files
    - **Validates: Requirements 3.1, 3.2**

  - [ ]* 6.5 Write property test for replacement affecting only generic wording
    - **Property 4: Replacement affects only generic wording**
    - Use Hypothesis with at least 100 iterations
    - **Validates: Requirements 3.3, 2.1, 2.2**

  - [ ]* 6.6 Write test for key and placeholder stability
    - **Property 5: Keys and placeholders are stable**
    - **Validates: Requirements 3.4**

  - [ ]* 6.7 Write property test for build round trip
    - **Property 6: Build round trip**
    - Include `build-messages.py --check` exiting 0 on the repository
    - **Validates: Requirements 4.1**

  - [ ]* 6.8 Write test for referenced i18n keys in settings.html
    - **Property 7: Referenced i18n keys exist**
    - **Validates: Requirements 4.2**

  - [ ]* 6.9 Write test for build error exit status
    - **Property 8: Build errors produce a non-zero exit**
    - Use a temporary copy of the catalog with invalid JSON; assert non-zero exit and unchanged messages.js
    - **Validates: Requirements 4.3**

  - [ ]* 6.10 Write test for untouched identifiers
    - **Property 9: Identifiers are untouched**
    - **Validates: Requirements 6.1**

- [ ] 7. Final checkpoint - Ensure all tests pass
  - Run the Python tests, `node tests/settings_releases_ui.cjs`, and `python scripts/build-messages.py --check`.
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties
- Unit tests validate specific examples and edge cases

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["2.1", "3.1", "3.2", "3.3", "3.4"] },
    { "id": 2, "tasks": ["2.2", "5.1", "5.2", "6.1"] },
    { "id": 3, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "6.7", "6.8", "6.9", "6.10"] }
  ]
}
```
