import { Component } from '@angular/core';
import { Shell } from './components/shell/shell';

@Component({
  selector: 'app-root',
  imports: [Shell],
  template: '<app-shell />',
})
export class App {}
