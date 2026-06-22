import { run, type TuiInput } from "./app"

const serverUrl = process.env.AH_SERVER_URL ?? "http://127.0.0.1:8822"
void run({ url: serverUrl })

export { run, type TuiInput }
