import { Component } from "react";
import { Panel } from "./States.jsx";

/**
 * Catches a render crash in one view so the rest of the page survives.
 *
 * This runs unattended on a wall display; an unexpected payload shape must not
 * leave a blank white page until someone notices and reloads. `resetKey`
 * clears the error when the user navigates elsewhere.
 */
export default class ErrorBoundary extends Component {
  state = { error: null, resetKey: this.props.resetKey };

  static getDerivedStateFromError(error) {
    return { error };
  }

  static getDerivedStateFromProps(props, state) {
    if (props.resetKey !== state.resetKey) {
      return { error: null, resetKey: props.resetKey };
    }
    return null;
  }

  componentDidCatch(error, info) {
    console.error("View crashed:", error, info?.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;

    return (
      <Panel className="border-alert/40 px-6 py-10 text-center">
        <p className="eyebrow text-alert">This view hit a problem</p>
        <p className="mt-3 text-sm text-ink">
          {this.state.error.message || "An unexpected error occurred."}
        </p>
        <p className="mx-auto mt-2 max-w-sm text-xs leading-relaxed text-ink-muted">
          Data collection is unaffected -- the poller runs in the backend
          service, not in this page.
        </p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="mt-5 rounded-lg border border-hairline px-4 py-2 text-sm font-medium text-ink transition-colors hover:bg-panel-raised"
        >
          Reload
        </button>
      </Panel>
    );
  }
}
