import csv
import io
from src.config.settings import ARTIFACTS


def safe_cell(value):
    if isinstance(value, str) and (value.lstrip().startswith(('=', '+', '-', '@'))
                                   or value.startswith(('\t', '\r', '\n'))):
        return "'" + value
    return value


def render_report(result, patrol=None):
    """Render the analysis result (and any saved patrol plan) as UTF-8 CSV text."""
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow([safe_cell(result['headline'])])
    writer.writerow([safe_cell(result.get('note', ''))])
    for metric in result['metrics']:
        writer.writerow([safe_cell(metric['label']), safe_cell(metric['value']), safe_cell(metric.get('detail', ''))])
    for table in result['tables']:
        writer.writerow([])
        writer.writerow([safe_cell(table['title'])])
        columns = table['columns']
        writer.writerow([safe_cell(column['label']) for column in columns])
        writer.writerows([safe_cell(row.get(column['key'])) for column in columns] for row in table['rows'])
    if patrol:
        writer.writerow([])
        writer.writerow([safe_cell(f"巡查排班 · {patrol['headline']}")])
        writer.writerow([safe_cell(patrol.get('note', ''))])
        for metric in patrol.get('metrics', []):
            writer.writerow([safe_cell(metric['label']), safe_cell(metric['value']), safe_cell(metric.get('detail', ''))])
        for table in patrol.get('tables', []):
            writer.writerow([])
            writer.writerow([safe_cell(table['title'])])
            columns = table['columns']
            writer.writerow([safe_cell(column['label']) for column in columns])
            writer.writerows([safe_cell(row.get(column['key'])) for column in columns] for row in table['rows'])
    return buffer.getvalue()


def write_report(run_id, attempt, result, patrol=None):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    name = f'analysis-{run_id}-{attempt}.csv'
    path = ARTIFACTS / name
    staging = path.with_suffix('.tmp')
    staging.write_text('﻿' + render_report(result, patrol), encoding='utf-8')
    staging.replace(path)
    return name
