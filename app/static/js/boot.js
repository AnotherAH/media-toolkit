// Runs before the modules: keeps a list of script errors for diagnostics
// (window.__errors) so a broken page can be inspected, and never throws.
(function () {
  var list = [];
  window.__errors = list;
  window.addEventListener('error', function (e) {
    list.push(String(e.message || e.error || 'error') + (e.filename ? ' @ ' + e.filename + ':' + e.lineno : ''));
  });
  window.addEventListener('unhandledrejection', function (e) {
    var r = e.reason;
    list.push('unhandled: ' + (r && (r.stack || r.message) || String(r)));
  });
})();
