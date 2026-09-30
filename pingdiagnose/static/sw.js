self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch (e) {
    d = { title: 'PingDiagnose', body: event.data ? event.data.text() : '' };
  }
  event.waitUntil((async () => {
    await self.registration.showNotification(d.title || 'PingDiagnose', {
      body: d.body || '',
      icon: '/static/icon.png',
      badge: '/static/icon.png',
      tag: d.tag || undefined,
      renotify: !!d.tag,
      requireInteraction: (d.title || '').startsWith('⚠'),
      data: { url: d.url || '/' },
    });
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    wins.forEach((c) => c.postMessage({ type: 'push', payload: d }));
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/', self.location.origin).href;
  event.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const win = wins.find((c) => c.url.startsWith(self.location.origin));
    if (win) {
      await win.focus();
      return win.navigate ? win.navigate(url).catch(() => undefined) : undefined;
    }
    return self.clients.openWindow(url);
  })());
});

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    const old = event.oldSubscription;
    let sub = event.newSubscription;
    const key = old && old.options && old.options.applicationServerKey;
    if (!sub && key) sub = await self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    if (!old || !sub) return;
    await fetch('/api/push/resubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ oldEndpoint: old.endpoint, subscription: sub.toJSON() }),
    });
  })().catch(() => undefined));
});
