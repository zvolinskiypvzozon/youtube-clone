// Типы API — из сгенерированного src/api/schema.d.ts (npm run gen:api). Здесь только
// короткие имена для используемых схем.
import type { components } from "./schema";

type Schemas = components["schemas"];

export type Me = Schemas["MeOut"];
export type RegisterIn = Schemas["RegisterIn"];
export type LoginIn = Schemas["LoginIn"];
export type ErrorOut = Schemas["ErrorOut"];
export type VideoListItem = Schemas["VideoListItem"];
export type VideoPage = Schemas["VideoPage"];
export type ChannelBrief = Schemas["ChannelBrief"];
