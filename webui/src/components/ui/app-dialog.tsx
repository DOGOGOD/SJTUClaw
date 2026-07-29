import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { CircleHelp, PencilLine, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";

export interface ConfirmDialogOptions {
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "destructive";
}

export interface PromptDialogOptions {
  title: string;
  description: string;
  label: string;
  placeholder?: string;
  defaultValue?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  required?: boolean;
  maxLength?: number;
}

interface AppDialogContextValue {
  confirmDialog: (options: ConfirmDialogOptions) => Promise<boolean>;
  promptDialog: (options: PromptDialogOptions) => Promise<string | null>;
}

type DialogRequest =
  | {
      id: number;
      kind: "confirm";
      options: ConfirmDialogOptions;
      resolve: (value: boolean) => void;
    }
  | {
      id: number;
      kind: "prompt";
      options: PromptDialogOptions;
      resolve: (value: string | null) => void;
    };

const AppDialogContext = createContext<AppDialogContextValue>({
  confirmDialog: async () => false,
  promptDialog: async () => null,
});

interface AppDialogViewProps {
  request: DialogRequest;
  onCancel: () => void;
  onConfirm: (value?: string) => void;
}

function AppDialogView({ request, onCancel, onConfirm }: AppDialogViewProps) {
  const isPrompt = request.kind === "prompt";
  const isDestructive = request.kind === "confirm" && request.options.variant === "destructive";
  const [value, setValue] = useState(isPrompt ? request.options.defaultValue || "" : "");
  const promptOptions = isPrompt ? request.options : null;
  const confirmDisabled = !!promptOptions?.required && !value.trim();
  const Icon = isPrompt ? PencilLine : isDestructive ? TriangleAlert : CircleHelp;

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onCancel(); }}>
      <DialogContent
        overlayClassName="z-[240] bg-foreground/15 backdrop-blur-[2px]"
        className="z-[240] w-[calc(100%-2rem)] max-w-[440px] gap-0 overflow-hidden rounded-2xl border-border bg-popover p-0 shadow-[0_24px_80px_hsl(215_30%_10%/0.22)]"
      >
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (!confirmDisabled) onConfirm(isPrompt ? value : undefined);
          }}
        >
          <div className="px-5 pb-5 pt-5 sm:px-6 sm:pt-6">
            <div className="flex items-start gap-3.5">
              <div
                className={cn(
                  "mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border",
                  isDestructive
                    ? "border-destructive/20 bg-destructive/10 text-destructive"
                    : "border-border/60 bg-secondary/70 text-foreground/75",
                )}
                aria-hidden="true"
              >
                <Icon className="h-4 w-4" strokeWidth={1.8} />
              </div>
              <DialogHeader className="min-w-0 flex-1 pr-6 text-left">
                <DialogTitle className="text-base leading-6">{request.options.title}</DialogTitle>
                <DialogDescription className="whitespace-pre-line text-xs leading-relaxed">
                  {request.options.description}
                </DialogDescription>
              </DialogHeader>
            </div>

            {promptOptions && (
              <div className="mt-5">
                <label
                  htmlFor={`app-dialog-input-${request.id}`}
                  className="mb-2 block text-[12px] font-medium text-foreground/85"
                >
                  {promptOptions.label}
                </label>
                <Input
                  id={`app-dialog-input-${request.id}`}
                  autoFocus
                  required={promptOptions.required}
                  maxLength={promptOptions.maxLength}
                  value={value}
                  placeholder={promptOptions.placeholder}
                  onChange={(event) => setValue(event.target.value)}
                  className="h-10 rounded-xl bg-background/70 px-3.5"
                />
              </div>
            )}
          </div>

          <DialogFooter className="gap-2 border-t border-border/60 bg-secondary/25 px-5 py-4 sm:space-x-0 sm:px-6">
            <Button type="button" variant="ghost" onClick={onCancel}>
              {request.options.cancelLabel || "取消"}
            </Button>
            <Button
              type="submit"
              variant={isDestructive ? "destructive" : "default"}
              disabled={confirmDisabled}
              className="sm:min-w-20"
            >
              {request.options.confirmLabel || "确定"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export function AppDialogProvider({ children }: { children: ReactNode }) {
  const [activeRequest, setActiveRequest] = useState<DialogRequest | null>(null);
  const activeRequestRef = useRef<DialogRequest | null>(null);
  const queueRef = useRef<DialogRequest[]>([]);
  const nextIdRef = useRef(0);

  const showNext = useCallback(() => {
    if (activeRequestRef.current || queueRef.current.length === 0) return;
    const next = queueRef.current.shift()!;
    activeRequestRef.current = next;
    setActiveRequest(next);
  }, []);

  const enqueue = useCallback((request: DialogRequest) => {
    queueRef.current.push(request);
    showNext();
  }, [showNext]);

  const confirmDialog = useCallback((options: ConfirmDialogOptions) => {
    return new Promise<boolean>((resolve) => {
      enqueue({
        id: ++nextIdRef.current,
        kind: "confirm",
        options,
        resolve,
      });
    });
  }, [enqueue]);

  const promptDialog = useCallback((options: PromptDialogOptions) => {
    return new Promise<string | null>((resolve) => {
      enqueue({
        id: ++nextIdRef.current,
        kind: "prompt",
        options,
        resolve,
      });
    });
  }, [enqueue]);

  const settle = useCallback((value?: string) => {
    const request = activeRequestRef.current;
    if (!request) return;
    activeRequestRef.current = null;
    setActiveRequest(null);
    if (request.kind === "prompt") request.resolve(value ?? null);
    else request.resolve(true);
    queueMicrotask(showNext);
  }, [showNext]);

  const cancel = useCallback(() => {
    const request = activeRequestRef.current;
    if (!request) return;
    activeRequestRef.current = null;
    setActiveRequest(null);
    if (request.kind === "prompt") request.resolve(null);
    else request.resolve(false);
    queueMicrotask(showNext);
  }, [showNext]);

  useEffect(() => {
    return () => {
      const pending = [activeRequestRef.current, ...queueRef.current].filter(Boolean) as DialogRequest[];
      activeRequestRef.current = null;
      queueRef.current = [];
      pending.forEach((request) => {
        if (request.kind === "prompt") request.resolve(null);
        else request.resolve(false);
      });
    };
  }, []);

  return (
    <AppDialogContext.Provider value={{ confirmDialog, promptDialog }}>
      {children}
      {activeRequest && (
        <AppDialogView
          key={activeRequest.id}
          request={activeRequest}
          onCancel={cancel}
          onConfirm={settle}
        />
      )}
    </AppDialogContext.Provider>
  );
}

export function useAppDialog() {
  return useContext(AppDialogContext);
}
