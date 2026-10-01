// No hay valores operativos predeterminados: Cloudflare es la única fuente.
export const BOT_KEYS = [
  "OBJ_SERVICIO", "OBJ_ZONA", "OBJ_DEPTO", "OBJ_MEDICO", "OBJ_PROFESIONAL",
  "OBJ_DOMICILIO", "OBJ_HORARIO_TURNO", "OBJ_DIAS_VALIDOS", "OBJ_FECHA_DISP",
  "OBJ_HORA_MIN", "OBJ_HORA_MAX", "OBJ_HORA_PRIORIDAD", "OBJ_HORA_FLEXIBLE",
  "OBJ_DIA_FLEXIBLE", "OBJ_FECHA_FLEXIBLE", "PORTAL_URL", "HEADLESS",
  "TIMEOUT_MS", "STOP_AFTER_LOGIN", "DRY_RUN",
] as const;
export type BotConfig = Record<(typeof BOT_KEYS)[number], string>;
type Variables = Partial<Env>;
const CLOCK = new Intl.DateTimeFormat("en-GB", {
  timeZone: "America/Argentina/Mendoza", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});
const BOOLEAN_KEYS = ["OBJ_HORA_FLEXIBLE", "OBJ_DIA_FLEXIBLE", "OBJ_FECHA_FLEXIBLE", "HEADLESS", "STOP_AFTER_LOGIN", "DRY_RUN"];
const OPTIONAL_KEYS = ["OBJ_MEDICO", "OBJ_PROFESIONAL", "OBJ_DOMICILIO", "OBJ_HORARIO_TURNO", "OBJ_DIAS_VALIDOS", "OBJ_FECHA_DISP"];

export function text(env: Variables, key: string, optional = false): string {
  const value = env[key as keyof Env];
  if (typeof value !== "string") {
    if (optional && value === undefined) return "";
    throw new Error(`config_invalid_${key}`);
  }
  const result = value.trim();
  if ((!optional && !result) || result.length > 250 || /[\u0000-\u001f\u007f]/.test(result)) {
    throw new Error(`config_invalid_${key}`);
  }
  return result;
}

export function minuteOfDay(value: string, key: string): number {
  if (!/^\d{1,2}:\d{2}$/.test(value)) throw new Error(`config_invalid_${key}`);
  const [hour, minute] = value.split(":").map(Number);
  if (hour > 23 || minute > 59) throw new Error(`config_invalid_${key}`);
  return hour * 60 + minute;
}

export function schedule(env: Variables) {
  const raw = text(env, "INTERVAL_MINUTES");
  const interval = Number(raw);
  if (!/^\d+$/.test(raw) || !Number.isInteger(interval) || interval < 1 || interval > 1440) {
    throw new Error("config_invalid_INTERVAL_MINUTES");
  }
  return { interval, start: minuteOfDay(text(env, "SCHEDULE_START"), "SCHEDULE_START"),
    end: minuteOfDay(text(env, "SCHEDULE_END"), "SCHEDULE_END") };
}

export function isBusinessSlot(time: number, plan: ReturnType<typeof schedule>): boolean {
  const parts = CLOCK.formatToParts(new Date(time));
  const minute = Number(parts.find(p => p.type === "hour")?.value) * 60 +
    Number(parts.find(p => p.type === "minute")?.value);
  const elapsed = (minute - plan.start + 1440) % 1440;
  const duration = (plan.end - plan.start + 1440) % 1440;
  return elapsed <= duration && elapsed % plan.interval === 0;
}

export function botConfig(env: Variables, forceDryRun = false): BotConfig {
  const config = Object.fromEntries(BOT_KEYS.map(key => [key, text(env, key,
    OPTIONAL_KEYS.includes(key))])) as BotConfig;
  for (const key of BOOLEAN_KEYS) if (!["true", "false"].includes(config[key as keyof BotConfig])) {
    throw new Error(`config_invalid_${key}`);
  }
  if (!["EARLIEST", "LATEST"].includes(config.OBJ_HORA_PRIORIDAD)) throw new Error("config_invalid_OBJ_HORA_PRIORIDAD");
  const min = !config.OBJ_HORA_MIN || config.OBJ_HORA_MIN === "false" ? 0 : minuteOfDay(config.OBJ_HORA_MIN, "OBJ_HORA_MIN");
  const max = !config.OBJ_HORA_MAX || config.OBJ_HORA_MAX === "false" ? 1439 : minuteOfDay(config.OBJ_HORA_MAX, "OBJ_HORA_MAX");
  if (min > max) throw new Error("config_invalid_OBJ_HORA_RANGE");
  const date = config.OBJ_FECHA_DISP;
  if (date && date !== "false") {
    const match = /^(\d{2})-(\d{2})-(\d{4})$/.exec(date);
    if (!match) throw new Error("config_invalid_OBJ_FECHA_DISP");
    const [, d, m, y] = match.map(Number);
    const parsed = new Date(Date.UTC(y, m - 1, d));
    if (parsed.getUTCFullYear() !== y || parsed.getUTCMonth() !== m - 1 || parsed.getUTCDate() !== d) throw new Error("config_invalid_OBJ_FECHA_DISP");
  }
  if (config.OBJ_DIAS_VALIDOS && config.OBJ_DIAS_VALIDOS.split(",").some(day =>
    !["LUNES", "MARTES", "MIERCOLES", "MIÉRCOLES", "JUEVES", "VIERNES", "SABADO", "SÁBADO", "DOMINGO"].includes(day.trim().toUpperCase()))) {
    throw new Error("config_invalid_OBJ_DIAS_VALIDOS");
  }
  if (config.PORTAL_URL !== "https://www.osep.mendoza.gov.ar/webapp_pri") throw new Error("config_invalid_PORTAL_URL");
  if (!/^\d+$/.test(config.TIMEOUT_MS) || Number(config.TIMEOUT_MS) < 1000 || Number(config.TIMEOUT_MS) > 120000) throw new Error("config_invalid_TIMEOUT_MS");
  if (forceDryRun) config.DRY_RUN = "true";
  return config;
}

export async function fingerprint(json: string): Promise<string> {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(json));
  return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
}
