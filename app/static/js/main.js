// Entry point: mounts every tab into the shell and runs the startup order.
import { start } from './shell.js';
import * as download from './download.js';
import * as transcript from './transcript.js';
import * as live from './live.js';
import * as queue from './queue.js';
import * as settings from './settings.js';
import * as wizard from './wizard.js';

start({ tabs: { download, transcript, live, queue, settings }, wizard });
