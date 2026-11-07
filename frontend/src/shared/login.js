/* 登录页：回填登录后的跳转地址，并在令牌错误时显示提示。 */
(function () {
  "use strict";
  const params = new URLSearchParams(location.search);
  const next = params.get("next");
  if (next && next.startsWith("/") && !next.startsWith("//"))
    document.getElementById("login-next").value = next;
  if (params.get("error")) document.getElementById("login-error").hidden = false;
})();
