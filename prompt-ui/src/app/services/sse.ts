import { Injectable, NgZone } from '@angular/core';
import { Observable } from 'rxjs';
import { SseMessage } from '../models';

@Injectable({ providedIn: 'root' })
export class SseService {
  constructor(private zone: NgZone) {}

  connect(url: string): Observable<SseMessage> {
    return new Observable(observer => {
      const es = new EventSource(url);
      es.onmessage = (e) => {
        this.zone.run(() => observer.next(JSON.parse(e.data)));
      };
      es.onerror = () => {
        this.zone.run(() => observer.error('SSE connection error'));
      };
      return () => es.close();
    });
  }
}
