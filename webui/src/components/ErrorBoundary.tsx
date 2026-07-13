import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

interface Props { children: ReactNode; }
interface State { hasError: boolean; error: Error | null; }

export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) { super(props); this.state = { hasError: false, error: null }; }
  static getDerivedStateFromError(error: Error): State { return { hasError: true, error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error("UI Error:", error, info.componentStack); }

  private _handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      const message = this.state.error?.message || "未知错误";
      const isNetworkLike = /服务器返回了网页|响应解析失败|请求超时|HTTP|fetch|NetworkError/i.test(message);
      return (
        <div className="flex h-full w-full items-center justify-center px-6 bg-background">
          <div className="max-w-sm text-center animate-enter-up">
            <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-amber-500/10 text-amber-600 dark:text-amber-400 select-none">
              <AlertTriangle className="h-7 w-7" />
            </div>
            <h2 className="text-lg font-semibold tracking-tight">界面出错了</h2>
            <p className="mt-2 text-sm text-muted-foreground/70">{message}</p>
            {isNetworkLike && (
              <p className="mt-1 text-xs text-muted-foreground/50">
                看起来是网络或后端连接问题，请检查后端服务是否已启动。
              </p>
            )}
            <div className="mt-5 flex items-center justify-center gap-3">
              <button
                onClick={this._handleRetry}
                className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-4 py-2 text-sm font-medium text-background hover:bg-foreground/90 transition-colors duration-200 active:scale-[0.98]"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                重试
              </button>
              <button
                onClick={() => window.location.reload()}
                className="inline-flex items-center rounded-lg border border-border px-4 py-2 text-sm font-medium text-foreground/80 hover:bg-secondary transition-colors duration-200 active:scale-[0.98]"
              >
                刷新页面
              </button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
