# UI 布局完整执行覆盖记录

来自 Chromium 真实页面计算样式；同一个控件在不同屏宽和语言下重复测量。控件级记录见 `artifacts/ui-layout/after/coverage.json`，案例摘要见 `coverage.csv`。

| 控件组／状态 | 语言 | 屏宽（px） | 案例数 | 控件测量数 | 结果 |
|---|---|---|---:|---:|---|
| `search-initial` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `search-results` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 408 | 通过 |
| `search-detail` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 420 | 通过 |
| `search-image` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 300 | 通过 |
| `search-video` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 300 | 通过 |
| `search-loading` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 408 | 通过 |
| `search-error` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 408 | 通过 |
| `admin-initial` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 180 | 通过 |
| `admin-workspace` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 720 | 通过 |
| `admin-subtitle` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 744 | 通过 |
| `admin-edit` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 792 | 通过 |
| `admin-play` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 768 | 通过 |
| `admin-analysis` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 756 | 通过 |
| `admin-upload` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 768 | 通过 |
| `admin-person-setup` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 792 | 通过 |
| `admin-many-long` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 3960 | 通过 |
| `admin-empty-segments` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 504 | 通过 |
| `persons-cast-candidates` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 2004 | 通过 |
| `persons-catalogue` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1644 | 通过 |
| `persons-expanded-roles` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1740 | 通过 |
| `persons-entity` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1824 | 通过 |
| `persons-role-fields` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1860 | 通过 |
| `persons-linked` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1848 | 通过 |
| `persons-capture` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1800 | 通过 |
| `persons-processing-review` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 456 | 通过 |
| `persons-single-review` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 732 | 通过 |
| `persons-batch-review` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 732 | 通过 |
| `settings-initial` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 204 | 通过 |
| `settings-analyzer-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-backend-openai` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-analyzer-backend-llama_cpp` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `settings-generator-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-generator-backend-openai` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-embedding-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-backend-mindex` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-backend-llama_cpp` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-embedding-backend-tokenhub` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |
| `settings-asr-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-backend-mindex` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-backend-llama_cpp` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-backend-openai` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-asr-backend-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 360 | 通过 |
| `settings-tts-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-backend-openai` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-backend-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-tts-backend-dashscope` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 396 | 通过 |
| `settings-person-custom` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-self` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-zhipu` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-deepseek` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-aliyun` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-tencent` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-minimax` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-person-backend-mindex` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 468 | 通过 |
| `settings-tuning` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 420 | 通过 |
| `settings-voice-register` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `settings-voice-url` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `settings-voice-upload` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `studio-initial` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 276 | 通过 |
| `studio-board` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 516 | 通过 |
| `studio-dialog-new` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `studio-dialog-copy` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `studio-dialog-rename` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 552 | 通过 |
| `studio-dialog-parameters` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 636 | 通过 |
| `studio-dialog-preflight` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 540 | 通过 |
| `studio-text-editor` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 780 | 通过 |
| `studio-query-image` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 732 | 通过 |
| `studio-query-video` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 732 | 通过 |
| `studio-query-text` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 780 | 通过 |
| `studio-stage-1` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1068 | 通过 |
| `studio-stage-2` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 912 | 通过 |
| `studio-stage-3` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1083 | 通过 |
| `studio-stage-4` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 912 | 通过 |
| `studio-window` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1104 | 通过 |
| `studio-candidate-player` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1113 | 通过 |
| `studio-version-detail` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1236 | 通过 |
| `studio-final` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1632 | 通过 |
| `studio-final-history` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1632 | 通过 |
| `studio-restore-plan` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1668 | 通过 |
| `studio-artifacts` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 1632 | 通过 |
| `review-film` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 36 | 通过 |
| `review-film-page2` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 36 | 通过 |
| `review-film-empty` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 36 | 通过 |
| `review-labels` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 1236 | 通过 |
| `review-labels-candidates` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 1236 | 通过 |
| `review-labels-player` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 1242 | 通过 |
| `review-labels-error` | zh | 1440, 1000, 800, 540, 390, 320 | 6 | 1236 | 通过 |
| `settings-many-profiles` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 2112 | 通过 |
| `settings-profile-key-clear` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 2292 | 通过 |
| `settings-profile-test-error` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 2112 | 通过 |
| `settings-clone-reference-text` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 564 | 通过 |
| `settings-empty-cloned-voice-list` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 588 | 通过 |
| `settings-manual-voice-fallback` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 588 | 通过 |
| `persons-empty-accepted` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 384 | 通过 |
| `persons-empty-rejected` | en / zh | 1440, 1000, 800, 540, 390, 320 | 12 | 372 | 通过 |

以上记录涵盖打开的真实界面，不代表每个界面都执行过全部数据和服务状态的笛卡尔组合。补充说明及未验证状态见 `docs/history/ui-layout-verification.md`。
