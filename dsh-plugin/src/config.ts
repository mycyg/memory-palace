/** Configuration for the service-only DeepSeek Harness adapter. */
import z from '@deepseek-ai/schemastery'

export interface Config {
  enabled: boolean
  injectWorkingSet: boolean
  writeFeed: boolean
}

export const Config: z<Partial<Config>, Config> = z.object({
  enabled: z.boolean().default(true).description('Enable MemoryPalace host events'),
  injectWorkingSet: z.boolean().default(true).description('Inject scoped context at session start'),
  writeFeed: z.boolean().default(true).description('Capture work observations in the service'),
})
