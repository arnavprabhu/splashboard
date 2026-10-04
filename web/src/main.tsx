import { render } from 'preact';
import './styles/tokens.css';
import './styles/components.css';
import { App, loginRedirect } from './app';
import { preloadInitial } from './routes';
import { installTheme, startStore } from './store';

installTheme();
preloadInitial();
startStore(loginRedirect);

const root = document.getElementById('app');
if (root) render(<App />, root);
