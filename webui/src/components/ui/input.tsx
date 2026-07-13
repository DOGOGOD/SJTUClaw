import * as React from "react";
import { cn } from "@/lib/utils";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-9 w-full rounded-lg border border-border/80 bg-transparent px-3 py-2 text-sm placeholder:text-muted-foreground/60 focus-visible:outline-none focus-visible:border-primary/50 focus-visible:ring-1 focus-visible:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-40 transition-colors duration-200",
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Input.displayName = "Input";

export interface TextareaProps extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, ...props }, ref) => {
    return (
      <textarea
        className={cn(
          "flex min-h-[80px] w-full rounded-lg border border-border/80 bg-transparent px-3 py-2.5 text-sm placeholder:text-muted-foreground/60 focus-visible:outline-none focus-visible:border-primary/50 focus-visible:ring-1 focus-visible:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-40 transition-colors duration-200 resize-y font-mono",
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Textarea.displayName = "Textarea";

export { Input, Textarea };
