// 保留选择器、声明顺序与既有 !important；仅检查可独立判断的 CSS 有效性。
export default {
  rules: {
    "color-no-invalid-hex": true,
    "declaration-block-no-duplicate-properties": [
      true,
      { ignore: ["consecutive-duplicates-with-different-values"] },
    ],
    "keyframe-block-no-duplicate-selectors": true,
    "no-invalid-double-slash-comments": true,
    "no-invalid-position-at-import-rule": true,
    "property-no-unknown": true,
    "selector-type-no-unknown": true,
    "unit-no-unknown": true,
  },
};
