import type { components } from "@/lib/api/schema";

type Schemas = components["schemas"];
export type Business = Schemas["BusinessOut"];
export type BusinessPage = Schemas["BusinessPage"];
export type Service = Schemas["ServiceOut"];
export type Availability = Schemas["AvailabilityOut"];
export type Slot = Schemas["SlotOut"];
export type Appointment = Schemas["AppointmentOut"];
export type AppointmentPage = Schemas["AppointmentPage"];
export type TokenDetail = Schemas["TokenWithStreamOut"];
export type ActiveToken = Schemas["ActiveTokenOut"];
export type Eta = Schemas["EtaOut"];
export type Token = Schemas["TokenOut"];
export type Queue = Schemas["QueueOut"];
export type QueueSnapshot = Schemas["SnapshotOut"];
export type SnapshotToken = Schemas["SnapshotToken"];
export type StreamToken = Schemas["StreamTokenOut"];
export type MyBusiness = Schemas["MyBusinessOut"];
