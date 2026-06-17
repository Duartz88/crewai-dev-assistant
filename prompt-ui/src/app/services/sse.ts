import { Injectable, NgZone } from '@angular/core';
import { Observable } from 'rxjs';
import { SseMessage } from '../models';

const RETRY_INITIAL_MS = 1_000;
const RETRY_MAX_MS     = 30_000;

@Injectable({ providedIn: 'root' })
export class SseService {
  constructor(private zone: NgZone) {}

  connect(url: string): Observable<SseMessage> {
    return new Observable(observer => {
      let es: EventSource | null = null;
      let retryMs    = RETRY_INITIAL_MS;
      let retryTimer: ReturnType<typeof setTimeout> | null = null;
      let closed     = false;

      const open = () => {
        es = new EventSource(url);

        es.onmessage = (e: MessageEvent) => {
          retryMs = RETRY_INITIAL_MS;   // reset backoff on successful message
          this.zone.run(() => {
            try { observer.next(JSON.parse(e.data) as SseMessage); }
            catch { /* ignore malformed frames — backend always sends valid JSON */ }
          });
        };

        es.onerror = () => {
          es?.close();
          es = null;
          if (closed) return;
          // Exponential backoff: 1s → 2s → 4s … capped at 30s
          retryTimer = setTimeout(() => {
            if (!closed) {
              retryMs = Math.min(retryMs * 2, RETRY_MAX_MS);
              open();
            }
          }, retryMs);
        };
      };

      open();

      // Teardown: called when the subscriber unsubscribes (e.g. component destroyed)
      return () => {
        closed = true;
        if (retryTimer !== null) clearTimeout(retryTimer);
        es?.close();
      };
    });
  }
}
