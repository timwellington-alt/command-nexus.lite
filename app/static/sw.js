/**
 * Command Nexus — Service Worker
 * Handles Web Push notifications for tech team alert routing.
 */

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(clients.claim()));

self.addEventListener('push', event => {
  if (!event.data) return;

  let payload = { title: 'Nexus Alert', body: '' };
  try { payload = event.data.json(); } catch { payload.body = event.data.text(); }

  const title = payload.title || 'Nexus Alert';

  // iOS WebKit does not support badge, vibrate, requireInteraction, or actions —
  // passing unsupported options causes showNotification to fail silently.
  // Keep options to the subset that works everywhere.
  const options = {
    body:  payload.body || '',
    icon:  '/static/img/icon-192.png',
    tag:   'nexus-alert',
    data:  { url: payload.url || '/network' },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/network';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin) && 'focus' in c) {
          c.navigate(url);
          return c.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
