#!/usr/bin/env node
// Fetch live Met Office weather and compute exact dates/ages for The Family Table.
// Config-driven: reads private config.json from --config <path> or %USERPROFILE%\AI\secrets\family_table\config.json.
// Usage: node fetch_metoffice.js [YYYY-MM-DD | --today | --tomorrow] [--config <path>]
const fs = require('fs');
const path = require('path');
const https = require('https');

function parseArgs(argv) {
  let targetArg = null;
  let configPath = null;
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--config' && argv[i + 1]) {
      configPath = argv[++i];
    } else if (!targetArg) {
      targetArg = argv[i];
    }
  }
  if (!configPath) {
    const home = process.env.USERPROFILE || process.env.HOME || '';
    configPath = path.join(home, 'AI', 'secrets', 'family_table', 'config.json');
  }
  return { targetArg, configPath };
}

function loadConfig(configPath) {
  if (configPath && fs.existsSync(configPath)) {
    return JSON.parse(fs.readFileSync(configPath, 'utf8'));
  }
  return {};
}

function tzParts(tz, date = new Date()) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    hour12: false
  }).formatToParts(date);
  const get = t => parts.find(p => p.type === t)?.value;
  return {
    year: Number(get('year')),
    month: Number(get('month')),
    day: Number(get('day')),
    hour: Number(get('hour'))
  };
}

function addDaysISO(iso, delta) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(Date.UTC(y, m - 1, d + delta, 12, 0, 0));
  return dt.toISOString().slice(0, 10);
}

function formatDateInfo(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const utcNoon = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
  const dowShort = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'short' }).format(utcNoon);
  const dowLong = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'long' }).format(utcNoon);
  const monthShort = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', month: 'short' }).format(utcNoon);
  const monthLong = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', month: 'long' }).format(utcNoon);
  const dowIdx = utcNoon.getUTCDay(); // 0 Sun .. 6 Sat
  return {
    iso,
    year: y,
    month: m,
    dayNum: d,
    dowIdx,
    isWeekend: dowIdx === 0 || dowIdx === 6,
    dowUpper: dowShort.toUpperCase().slice(0, 3),
    dowShort,
    dowLong,
    dateLong: `${dowLong} ${d} ${monthLong} ${y}`,
    subjectDate: `${dowShort} ${d} ${monthShort}`
  };
}

function computeChildTags(iso, children) {
  const [y, m, d] = iso.split('-').map(Number);
  const out = [];
  for (const ch of children || []) {
    if (!ch || !ch.dob) continue;
    const [by, bm, bd] = String(ch.dob).split('-').map(Number);
    let tag = '';
    if (ch.age_mode === 'months_under_36') {
      let months = (y - by) * 12 + (m - bm);
      if (d < bd) months -= 1;
      tag = months < 36 ? `${months} months` : `Age ${Math.floor(months / 12)}`;
    } else {
      let years = y - by;
      if (m < bm || (m === bm && d < bd)) years -= 1;
      tag = ch.class_label ? `Age ${years} · ${ch.class_label}` : `Age ${years}`;
    }
    out.push({ name: ch.name, tag });
  }
  return out;
}

function fetchHtml(url, attempt = 1) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      timeout: 20000,
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Accept-Language': 'en-GB,en;q=0.9'
      }
    }, res => {
      if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
        return resolve(fetchHtml(new URL(res.headers.location, url).href, attempt));
      }
      if (res.statusCode !== 200) {
        return reject(new Error(`Met Office HTTP ${res.statusCode}`));
      }
      let body = '';
      res.setEncoding('utf8');
      res.on('data', c => body += c);
      res.on('end', () => resolve(body));
    });
    req.on('timeout', () => req.destroy(new Error('Met Office request timed out')));
    req.on('error', err => {
      if (attempt < 3) {
        setTimeout(() => resolve(fetchHtml(url, attempt + 1)), 2000 * attempt);
      } else {
        reject(err);
      }
    });
  });
}

function parseSnapshot(html, iso) {
  const snapRegex = new RegExp(`data-date="${iso}"[\\s\\S]*?<\\/ul>\\s*<\\/div>`, 'i');
  const m = html.match(snapRegex);
  if (!m) return null;
  const block = m[0];
  const tempM = block.match(/(-?\d+)°C/);
  const descM = block.match(/class="[^"]*snapshot-weather-description[^"]*"[^>]*>([^<]+)<\/div>/i);
  const rainM = block.match(/<span>\s*Rain\s*([^<]+)<\/span>/i);
  let rain = rainM ? rainM[1].trim().replace(/&lt;/g, '<') : null;
  if (rain === '<5%') rain = 'under 5%';
  return {
    high: tempM ? `${tempM[1]}°` : null,
    summary: descM ? descM[1].trim() : null,
    rain
  };
}

function parseHourly(html, iso) {
  const tableRegex = new RegExp(`<table class="[^"]*forecast-table hourly-table[^"]*" data-date="${iso}">([\\s\\S]*?)<\\/table>`, 'i');
  const m = html.match(tableRegex);
  if (!m) return { at8: null, dailyMin: null };
  const tableHtml = m[1];
  const timeRowMatch = tableHtml.match(/<tr class="step-time[^>]*>([\s\S]*?)<\/tr>/i);
  const tempRowMatch = tableHtml.match(/<tr class="[^"]*weather-temperature-row[^"]*"[^>]*>([\s\S]*?)<\/tr>/i);
  if (!timeRowMatch || !tempRowMatch) return { at8: null, dailyMin: null };

  const times = [...timeRowMatch[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)]
    .map(x => x[1].replace(/<[^>]+>/g, '').trim());
  const temps = [...tempRowMatch[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)]
    .map(x => x[1].replace(/<[^>]+>/g, '').trim());

  const idx8 = times.indexOf('8am');
  let at8 = idx8 !== -1 && temps[idx8] ? temps[idx8] : null;
  const numericTemps = temps.map(t => parseInt(t, 10)).filter(n => !Number.isNaN(n));
  const dailyMin = numericTemps.length ? `${Math.min(...numericTemps)}°` : null;
  if (!at8 && temps.length) at8 = temps[0];
  return { at8, dailyMin };
}

function parseSunTimes(html, iso) {
  const format12 = (hh, mm) => {
    const h = parseInt(hh, 10);
    const suffix = h >= 12 ? 'pm' : 'am';
    const h12 = h % 12 === 0 ? 12 : h % 12;
    return `${h12}:${mm} ${suffix}`;
  };

  const rangeRegex = new RegExp(
    `<div class="sun-rise-range">\\s*<time datetime="${iso}T[^"]*">(\\d{2}):(\\d{2})<\\/time>\\s*<time datetime="${iso}T[^"]*">(\\d{2}):(\\d{2})<\\/time>`,
    'i'
  );
  const specific = html.match(rangeRegex);
  if (specific) {
    return {
      sunrise: format12(specific[1], specific[2]),
      sunset: format12(specific[3], specific[4])
    };
  }

  const anyRange = html.match(/<div class="sun-rise-range">\s*<time datetime="[^"]*">(\d{2}):(\d{2})<\/time>\s*<time datetime="[^"]*">(\d{2}):(\d{2})<\/time>/i);
  if (anyRange) {
    return {
      sunrise: format12(anyRange[1], anyRange[2]),
      sunset: format12(anyRange[3], anyRange[4])
    };
  }
  return { sunrise: null, sunset: null };
}

(async () => {
  const { targetArg, configPath } = parseArgs(process.argv);
  const cfg = loadConfig(configPath);
  const tz = cfg.timezone || 'Europe/London';
  const geohash = cfg.met_office_geohash || 'gcpvj0duq';
  const metUrl = `https://weather.metoffice.gov.uk/forecast/${geohash}`;
  const nowParts = tzParts(tz);
  const todayIso = `${nowParts.year}-${String(nowParts.month).padStart(2, '0')}-${String(nowParts.day).padStart(2, '0')}`;

  let targetIso;
  if (targetArg === '--today') {
    targetIso = todayIso;
  } else if (targetArg === '--tomorrow') {
    targetIso = addDaysISO(todayIso, 1);
  } else if (targetArg && /^\d{4}-\d{2}-\d{2}$/.test(targetArg)) {
    targetIso = targetArg;
  } else {
    targetIso = nowParts.hour >= 12 ? addDaysISO(todayIso, 1) : todayIso;
  }

  const nextIso = addDaysISO(targetIso, 1);
  const end28Iso = addDaysISO(targetIso, 28);
  const targetInfo = formatDateInfo(targetIso);
  const nextInfo = formatDateInfo(nextIso);
  const childrenTags = computeChildTags(targetIso, cfg.children);
  const pickups = cfg.default_pickups || {};

  const daySpan = targetInfo.isWeekend ? (targetInfo.dowIdx === 6 ? 3 : 2) : 7;
  const weekDays = Array.from({ length: daySpan }, (_, i) => {
    const dIso = addDaysISO(targetIso, i);
    const info = formatDateInfo(dIso);
    return {
      iso: dIso,
      day: info.dowUpper,
      num: String(info.dayNum),
      weekday: info.dowLong,
      default_pickup: pickups[String(info.dowIdx)] || ''
    };
  });

  const html = await fetchHtml(metUrl);
  const snap = parseSnapshot(html, targetIso) || {};
  const hourly = parseHourly(html, targetIso);
  const sun = parseSunTimes(html, targetIso);
  const nextSnap = parseSnapshot(html, nextIso);
  const nextHourly = parseHourly(html, nextIso);

  const weather = {
    high: snap.high,
    summary: snap.summary,
    at8: hourly.at8 || hourly.dailyMin,
    daily_min: hourly.dailyMin,
    rain: snap.rain || 'under 5%',
    sunrise: sun.sunrise,
    sunset: sun.sunset,
    tomorrow: nextSnap && nextSnap.high
      ? `${nextInfo.dowLong}: ${nextSnap.high}${nextSnap.summary ? ' · ' + nextSnap.summary : ''}`
      : '',
    next_day_detail: nextSnap ? {
      date: nextIso,
      weekday: nextInfo.dowLong,
      high: nextSnap.high,
      summary: nextSnap.summary,
      at8: nextHourly.at8 || nextHourly.dailyMin,
      rain: nextSnap.rain || 'under 5%'
    } : null,
    source: cfg.weather_source_label || 'Met Office · London'
  };

  console.log(JSON.stringify({
    file_date: targetIso,
    date_long: targetInfo.dateLong,
    subject_date: targetInfo.subjectDate,
    dow_idx: targetInfo.dowIdx,
    is_weekend: targetInfo.isWeekend,
    edition_label: targetInfo.isWeekend ? 'Weekend edition' : 'Breakfast edition',
    week_title: targetInfo.isWeekend ? 'The weekend ahead' : 'The week ahead',
    next_edition: `${nextInfo.dowLong}, ${cfg.next_edition_time || '8:00 am'}`,
    children: childrenTags,
    calendar_window: {
      start: `${targetIso}T00:00:00Z`,
      week_end: `${addDaysISO(targetIso, 6)}T23:59:59Z`,
      end: `${end28Iso}T23:59:59Z`
    },
    week_days: weekDays,
    weather
  }, null, 2));
})().catch(err => {
  console.error(JSON.stringify({ error: err.message }));
  process.exit(1);
});
