import { Component } from 'react';
import type { ReactNode } from 'react';
import Icon from './Icons';

type Props = { children: ReactNode; onReady: (ready: boolean) => void };

/** Keep the site form usable if a deployment or connection interrupts the map module. */
export default class MapErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch() {
    this.props.onReady(false);
  }

  render() {
    if (this.state.failed) {
      return <div className="map-module-error" role="alert">
        <Icon name="info" size={26} />
        <h2>Your map couldn’t open.</h2>
        <p>A connection issue or a recent update may have interrupted it. Reload the page to try again.</p>
        <button type="button" className="button secondary" onClick={() => window.location.reload()}>Reload page</button>
        <p className="reload-note">Reloading restarts this session. Save your summary first if one is available.</p>
      </div>;
    }
    return this.props.children;
  }
}
