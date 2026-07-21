// Daisy artifact-proxy runtime.
//
// Injected into every page served through /artifact-proxy so that URLs built at
// runtime by the page's own scripts (fetch/XHR, history navigations) resolve
// against the real origin and keep flowing through the proxy, and so a
// cross-origin history.replaceState no longer throws.
//
// BASE (the page's real origin) and PROXY (our proxy prefix) are filled in
// per-page by the server, which substitutes the two placeholder tokens below
// with JSON-encoded string literals before injecting this script.
(function () {
  var BASE = __DAISY_PROXY_BASE__;
  var PROXY = __DAISY_PROXY_URL__;
  var WS_PROXY = __DAISY_WS_PROXY_URL__;

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

  // A WebSocket URL routed to the same-origin bridge endpoint. A relative or
  // http(s) URL (resolved against the real origin) is normalized to ws(s) first,
  // then wrapped so the browser opens the socket to us and we relay it upstream.
  function wsProx(url) {
    if (typeof url !== "string" || !url) {
      return url;
    }
    try {
      var absolute = new URL(url, BASE);
      if (absolute.protocol === "http:") {
        absolute.protocol = "ws:";
      } else if (absolute.protocol === "https:") {
        absolute.protocol = "wss:";
      }
      if (!/^wss?:$/i.test(absolute.protocol)) {
        return url;
      }
      var wsBase = window.location.origin.replace(/^http/i, "ws");
      return wsBase + WS_PROXY + encodeURIComponent(absolute.href);
    } catch (error) {
      return url;
    }
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

  // Route WebSocket connections through the same-origin bridge so realtime features
  // (live scores, chat, telemetry) work from the framed page instead of being
  // blocked as cross-origin.
  if (window.WebSocket) {
    var NativeWebSocket = window.WebSocket;
    var ProxiedWebSocket = function (url, protocols) {
      var target = wsProx(url);
      return protocols === undefined
        ? new NativeWebSocket(target)
        : new NativeWebSocket(target, protocols);
    };
    ProxiedWebSocket.prototype = NativeWebSocket.prototype;
    ["CONNECTING", "OPEN", "CLOSING", "CLOSED"].forEach(function (key) {
      try {
        ProxiedWebSocket[key] = NativeWebSocket[key];
      } catch (error) {
        // Read-only in some engines; the prototype still carries the constants.
      }
    });
    window.WebSocket = ProxiedWebSocket;
  }

  // Server-sent events are plain GETs, so they route through the HTTP proxy.
  if (window.EventSource) {
    var NativeEventSource = window.EventSource;
    var ProxiedEventSource = function (url, config) {
      return new NativeEventSource(prox(url), config);
    };
    ProxiedEventSource.prototype = NativeEventSource.prototype;
    window.EventSource = ProxiedEventSource;
  }

  // Analytics/telemetry beacons, so a page's fire-and-forget POSTs still resolve.
  if (navigator.sendBeacon) {
    var nativeSendBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      return nativeSendBeacon(prox(url), data);
    };
  }
})();
