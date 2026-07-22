import { useCallback, useEffect, useRef, useState } from "react";
import { Check, MonitorUp, PawPrint, Plus, Power, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  closePet,
  deletePet,
  fetchPetSettings,
  fetchPets,
  openPet,
  savePetSettings,
  uploadPet,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { usePetSelection } from "@/contexts/PetSelectionContext";
import type { PetInfo, PetSettings } from "@/lib/types";

interface PetRuntimeResult {
  settings: PetSettings;
  running: boolean;
}

function errorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "操作失败，请稍后重试";
  try {
    const parsed = JSON.parse(error.message) as { detail?: string };
    return parsed.detail || error.message;
  } catch {
    return error.message;
  }
}

function SettingSwitch({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-45",
        checked ? "bg-primary" : "bg-muted ring-1 ring-inset ring-border",
      )}
    >
      <span
        className={cn(
          "absolute left-[3px] top-[3px] h-4 w-4 rounded-full bg-background shadow-sm ring-1 ring-border/35 transition-transform duration-200",
          checked ? "translate-x-[22px]" : "translate-x-0",
        )}
      />
    </button>
  );
}

function PetPreview({ pet }: { pet: PetInfo }) {
  return (
    <div className="relative aspect-[192/208] w-[92px] shrink-0 overflow-hidden" aria-hidden="true">
      <img
        src={pet.spritesheetUrl}
        alt=""
        className="pointer-events-none absolute left-0 top-0 h-auto w-[800%] max-w-none select-none"
        draggable={false}
      />
    </div>
  );
}

export function PetSettingsSection() {
  const { setSelectedPet } = usePetSelection();
  const [settings, setSettings] = useState<PetSettings | null>(null);
  const [pets, setPets] = useState<PetInfo[]>([]);
  const [running, setRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [settingsResult, petsResult] = await Promise.all([
        fetchPetSettings(),
        fetchPets(),
      ]);
      setSettings(settingsResult.settings);
      setRunning(settingsResult.running);
      setPets(petsResult.pets || []);
      const selected = petsResult.pets.find(
        (pet) => pet.id === settingsResult.settings.selectedPetId,
      );
      if (selected) setSelectedPet(selected);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [setSelectedPet]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const runMutation = async (
    action: () => Promise<PetRuntimeResult>,
    successMessage: string,
    onSuccess?: (result: PetRuntimeResult) => void,
  ) => {
    setBusy(true);
    setError("");
    try {
      const result = await action();
      setSettings(result.settings);
      setRunning(result.running);
      onSuccess?.(result);
      setStatus(successMessage);
      window.setTimeout(() => setStatus(""), 2200);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const handleSelect = (pet: PetInfo) => {
    if (!settings || settings.selectedPetId === pet.id || busy) return;
    void runMutation(
      () => savePetSettings({ selectedPetId: pet.id }),
      `已选择 ${pet.displayName}`,
      () => setSelectedPet(pet),
    );
  };

  const handleDelete = async (pet: PetInfo) => {
    if (pet.readOnly || busy || !confirm(`确定删除宠物“${pet.displayName}”吗？`)) return;
    setBusy(true);
    setError("");
    try {
      await deletePet(pet.id);
      await refresh();
      setStatus("宠物已删除");
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div aria-label="正在加载宠物设置" className="space-y-4">
        <div className="skeleton h-7 w-36 rounded-md" />
        <div className="skeleton h-16 w-full rounded-xl" />
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="skeleton h-36 rounded-xl" />
          <div className="skeleton h-36 rounded-xl" />
        </div>
      </div>
    );
  }

  if (!settings) {
    return (
      <div>
        <h2 className="text-xl font-semibold tracking-[-0.025em]">桌面宠物</h2>
        <p className="mt-2 text-sm text-destructive">{error || "无法加载宠物设置"}</p>
        <Button className="mt-4" size="sm" variant="outline" onClick={() => void refresh()}>
          重新加载
        </Button>
      </div>
    );
  }

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold tracking-[-0.025em]">桌面宠物</h2>
          <p className="mt-1.5 max-w-xl text-[13px] leading-relaxed text-muted-foreground">
            选择陪伴你的角色，并控制宠物窗口的启动方式。
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground" aria-live="polite">
          <span className={cn("h-2 w-2 rounded-full", running ? "bg-emerald-500" : "bg-muted-foreground/35")} />
          {running ? "正在运行" : "已关闭"}
        </div>
      </div>

      <div className="mt-7 divide-y divide-border/50 rounded-xl border border-border/60 bg-card/35 px-4">
        <div className="flex items-center justify-between gap-5 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <Power className="h-4 w-4 shrink-0 text-muted-foreground" />
            <div>
              <p className="text-sm font-medium">显示桌面宠物</p>
              <p className="mt-0.5 text-xs text-muted-foreground">立即开启或关闭置顶宠物窗口</p>
            </div>
          </div>
          <SettingSwitch
            checked={settings.enabled && running}
            disabled={busy}
            label="显示桌面宠物"
            onChange={(checked) => void runMutation(checked ? openPet : closePet, checked ? "宠物已开启" : "宠物已关闭")}
          />
        </div>
        <div className="flex items-center justify-between gap-5 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <MonitorUp className="h-4 w-4 shrink-0 text-muted-foreground" />
            <div>
              <p className="text-sm font-medium">随 Gateway 启动</p>
              <p className="mt-0.5 text-xs text-muted-foreground">Gateway 启动后自动恢复宠物窗口</p>
            </div>
          </div>
          <SettingSwitch
            checked={settings.launchOnGatewayStart}
            disabled={busy}
            label="随 Gateway 启动"
            onChange={(checked) => void runMutation(
              () => savePetSettings({ launchOnGatewayStart: checked }),
              checked ? "已启用自动启动" : "已关闭自动启动",
            )}
          />
        </div>
      </div>

      <div className="mt-8 flex items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold">宠物角色</h3>
          <p className="mt-1 text-xs text-muted-foreground">选择后会立即应用到桌面窗口</p>
        </div>
        <AddPetDialog
          disabled={busy}
          onAdded={async (replyGeneration) => {
            await refresh();
            setStatus(
              replyGeneration.source === "llm"
                ? `新宠物已添加，并生成了 ${replyGeneration.count} 条专属互动台词`
                : `新宠物已添加。${replyGeneration.warning}`,
            );
          }}
        />
      </div>

      {pets.length === 0 ? (
        <div className="mt-4 rounded-xl border border-dashed border-border px-5 py-10 text-center">
          <PawPrint className="mx-auto h-6 w-6 text-muted-foreground/50" />
          <p className="mt-3 text-sm font-medium">还没有可用宠物</p>
          <p className="mt-1 text-xs text-muted-foreground">添加一张 Codex v1 或 v2 spritesheet</p>
        </div>
      ) : (
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          {pets.map((pet) => {
            const selected = settings.selectedPetId === pet.id;
            return (
              <div
                key={pet.id}
                className={cn(
                  "relative flex min-h-36 overflow-hidden rounded-xl border bg-card/45 transition-colors",
                  selected ? "border-primary/65 bg-primary/[0.045]" : "border-border/60 hover:border-border",
                )}
              >
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => handleSelect(pet)}
                  className="flex min-w-0 flex-1 items-center gap-3 p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring/50"
                >
                  <PetPreview pet={pet} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5 text-sm font-semibold">
                      {pet.displayName}
                      {selected && <Check className="h-3.5 w-3.5 text-primary" aria-label="当前选择" />}
                    </span>
                    <span className="mt-1 block text-xs leading-relaxed text-muted-foreground">
                      {pet.description || "自定义桌面宠物"}
                    </span>
                    {!pet.readOnly && (
                      <span className="mt-2 block text-[10px] text-muted-foreground/60">
                        自定义宠物
                      </span>
                    )}
                  </span>
                </button>
                {!pet.readOnly && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    disabled={busy}
                    className="absolute right-2 top-2 text-muted-foreground hover:text-destructive"
                    onClick={() => void handleDelete(pet)}
                    title={`删除 ${pet.displayName}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {(status || error) && (
        <p
          className={cn("mt-4 text-xs", error ? "text-destructive" : "text-muted-foreground")}
          role={error ? "alert" : "status"}
        >
          {error || status}
        </p>
      )}
    </div>
  );
}

function AddPetDialog({
  disabled,
  onAdded,
}: {
  disabled: boolean;
  onAdded: (replyGeneration: {
    source: "llm" | "fallback";
    count: number;
    warning: string;
  }) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const reset = () => {
    setFile(null);
    setUploadError("");
    if (fileRef.current) fileRef.current.value = "";
  };

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen && uploading) return;
    setOpen(nextOpen);
    if (!nextOpen) reset();
  };

  const handleUpload = async () => {
    if (!file) {
      setUploadError("请选择宠物 ZIP 压缩包");
      return;
    }
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setUploadError("宠物包仅支持 ZIP 格式");
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      setUploadError("宠物包超过 50 MB 限制");
      return;
    }
    setUploading(true);
    setUploadError("");
    try {
      const result = await uploadPet(file);
      await onAdded(result.replyGeneration);
      setOpen(false);
      reset();
    } catch (err) {
      setUploadError(errorMessage(err));
    } finally {
      setUploading(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline" disabled={disabled}>
          <Plus className="mr-1.5 h-3.5 w-3.5" />
          添加宠物
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加桌面宠物</DialogTitle>
          <DialogDescription>
            上传包含 pet.json 和 spritesheet 的 ZIP 宠物包。导入成功后会根据 description 自动生成专属互动台词。
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-2">
          <label className="grid gap-1.5 text-xs font-medium">
            宠物压缩包
            <span className="flex min-h-20 cursor-pointer items-center justify-center gap-2 rounded-xl border border-dashed border-border bg-muted/25 px-4 text-center text-xs font-normal text-muted-foreground transition-colors hover:border-primary/45 hover:bg-primary/[0.025]">
              <Upload className="h-4 w-4 shrink-0" />
              {file ? file.name : "选择 .zip 文件"}
              <input
                ref={fileRef}
                type="file"
                accept="application/zip,.zip"
                className="sr-only"
                onChange={(event) => {
                  setFile(event.target.files?.[0] || null);
                  setUploadError("");
                }}
              />
            </span>
            <span className="font-normal leading-relaxed text-muted-foreground">
              包内仅允许 pet.json 与 spritesheet.webp（或 spritesheet.png），可置于 ZIP 根目录或与宠物 ID 同名的顶层目录。
            </span>
          </label>
          {uploadError && (
            <p className="text-xs text-destructive" role="alert">
              {uploadError}
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => handleOpenChange(false)} disabled={uploading}>取消</Button>
          <Button onClick={() => void handleUpload()} disabled={uploading}>
            {uploading ? "正在导入并生成台词..." : "添加"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
