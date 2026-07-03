// Harness preview-proxy runtime.
//
// Injected into every page served through /preview-proxy so that URLs built at
// runtime by the page's own scripts (fetch/XHR, history navigations) resolve
// against the real origin and keep flowing through the proxy, and so a
// cross-origin history.replaceState no longer throws.
//
// BASE (the page's real origin) and PROXY (our proxy prefix) are filled in
// per-page by the server, which substitutes the two placeholder tokens below
// with JSON-encoded string literals before injecting this script.
(function () {
  var BASE = __HARNESS_PROXY_BASE__;
  var PROXY = __HARNESS_PROXY_URL__;

  function abs(url) {
    try {
      return new URL(url, BASE).href;
    } catch (error) {
      return null;
    }
  }

  function prox(url) {
    if (typeof url !== "string" || !url) {
      return url;
    }
    if (/^(data:|blob:|javascript:|about:|mailto:|tel:|#)/i.test(url)) {
      return url;
    }
    if (url.indexOf(PROXY) !== -1) {
      return url;
    }
    var absolute = abs(url);
    if (!absolute || !/^https?:/i.test(absolute)) {
      return url;
    }
    return PROXY + encodeURIComponent(absolute);
  }

  // Swallow the cross-origin throw some pages hit when calling history APIs from
  // inside the framed proxy.
  ["pushState", "replaceState"].forEach(function (method) {
    var original = history[method];
    history[method] = function (state, title, url) {
      try {
        return original.call(history, state, title, url);
      } catch (error) {
        try {
          return original.call(history, state, title);
        } catch (ignored) {
          // Both signatures failed; drop the navigation rather than throw.
        }
      }
    };
  });

  if (window.fetch) {
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
      try {
        if (typeof input === "string") {
          input = prox(input);
        } else if (input && input.url) {
          input = new Request(prox(input.url), input);
        }
      } catch (error) {
        // Leave the original input untouched if it cannot be proxied.
      }
      return originalFetch.call(this, input, init);
    };
  }

  if (window.XMLHttpRequest) {
    var originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
      try {
        url = prox(url);
      } catch (error) {
        // Leave the original URL untouched if it cannot be proxied.
      }
      return originalOpen.apply(this, [method, url].concat([].slice.call(arguments, 2)));
    };
  }
})();
