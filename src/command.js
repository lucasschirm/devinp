import {Command, Flags} from '@oclif/core'

import {arrangeRows, buildRows, GROUP_IDS, SORT_IDS} from './lib/models.js'
import {renderTable} from './lib/render-table.js'
import {loadScores} from './lib/scores.js'
import {startServer} from './lib/server.js'
import {loadModelData} from './lib/source.js'

export default class Devinp extends Command {
  static summary = 'Show Devin model prices in a pretty table'
  static description = [
    'Reads prices from `devin models list --format json` (or a saved snapshot via',
    '--source), joins BenchLM coding scores from data/benchlm-scores.json, and',
    'renders a sorted pricing table. Thinking-effort variants that share a price',
    'are collapsed into one row.',
  ].join('\n')

  static examples = [
    '<%= config.bin %>',
    '<%= config.bin %> -o=input',
    '<%= config.bin %> -o=bench -g=family',
    '<%= config.bin %> --web --port 8080',
    '<%= config.bin %> --source sample/models.json',
  ]

  static flags = {
    order: Flags.string({
      char: 'o',
      options: SORT_IDS,
      default: 'value',
      summary: 'sort by input | cached | output price, bench score, or value (Bench ÷ output)',
    }),
    group: Flags.string({
      char: 'g',
      options: GROUP_IDS,
      summary: 'group rows into sections by family | price | context size',
    }),
    web: Flags.boolean({
      summary: 'serve the table on a local webpage instead of printing',
      default: false,
      exclusive: ['json'],
    }),
    port: Flags.integer({
      summary: 'port for --web',
      default: 4321,
      dependsOn: ['web'],
    }),
    source: Flags.string({
      summary: 'load prices from a saved `devin models list --format json` file',
    }),
    json: Flags.boolean({
      summary: 'print normalized rows as JSON instead of a table',
      default: false,
    }),
  }

  async run() {
    const {flags} = await this.parse(Devinp)

    let data
    try {
      data = await loadModelData({source: flags.source})
    } catch (error) {
      this.error(error instanceof Error ? error.message : String(error), {exit: 1})
    }

    const scores = await loadScores(this.config.root)
    const rows = buildRows(data, scores)

    if (flags.json) {
      this.log(JSON.stringify(rows, null, 2))
      return
    }

    if (flags.web) {
      await startServer({rows, benchMeta: scores.meta, port: flags.port, log: (m) => this.log(m)})
      return
    }

    const sections = arrangeRows(rows, {order: flags.order, group: flags.group})
    this.log(renderTable(sections, {benchMeta: scores.meta}))
  }
}
