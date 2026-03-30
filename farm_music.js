/**
 * JACOB'S MONEY FARM — AMBIENT MUSIC v2
 * Rich procedural bossa nova with:
 *   - Jazz chord voicings (maj7, min7, dom7, min9)
 *   - Melodic lead line (nylon guitar feel)
 *   - Walking bass lines per chord
 *   - Rimshot + hi-hat bossa nova clave rhythm
 *   - Convolution reverb
 *   - Three sections: A (main), B (bridge), C (breakdown)
 * No external CDN — works always, zero cost
 */
(function() {
  const KEY_VOL  = 'farm_music_vol';
  const KEY_MUTE = 'farm_music_mute';

  let ctx = null, playing = false;
  let vol   = parseFloat(localStorage.getItem(KEY_VOL)  || '0.18');
  let muted = localStorage.getItem(KEY_MUTE) === '1';
  let masterGain = null, reverbNode = null, dryGain = null, wetGain = null;
  let scheduledNodes = [];
  let barCount = 0;
  let lookahead = null;

  const BPM  = 84;
  const BEAT = 60 / BPM;
  const BAR  = BEAT * 4;

  // Jazz chord voicings: bass notes (low) + guitar notes (mid)
  const CHORDS = {
    A: [
      { bass:[65.41,98.00],  notes:[261.63,329.63,392.00,493.88,523.25] },  // Cmaj7
      { bass:[55.00,82.41],  notes:[220.00,261.63,329.63,392.00,440.00] },  // Am7
      { bass:[73.42,110.00], notes:[293.66,349.23,440.00,523.25,587.33] },  // Dm7
      { bass:[49.00,73.42],  notes:[392.00,493.88,587.33,659.26,783.99] },  // G7
      { bass:[65.41,98.00],  notes:[261.63,329.63,392.00,493.88,523.25] },  // Cmaj7
      { bass:[82.41,123.47], notes:[329.63,392.00,493.88,587.33,659.26] },  // Em7
      { bass:[55.00,82.41],  notes:[220.00,261.63,329.63,392.00,440.00] },  // Am7
      { bass:[49.00,73.42],  notes:[392.00,523.25,587.33,659.26,783.99] },  // G7sus4
    ],
    B: [
      { bass:[87.31,130.81], notes:[349.23,440.00,523.25,659.26,698.46] },  // Fmaj7
      { bass:[82.41,123.47], notes:[329.63,392.00,493.88,587.33,659.26] },  // Em7
      { bass:[73.42,110.00], notes:[293.66,369.99,440.00,523.25,659.26] },  // Dm9
      { bass:[49.00,73.42],  notes:[392.00,493.88,587.33,698.46,880.00] },  // G13
      { bass:[87.31,130.81], notes:[349.23,440.00,523.25,659.26,698.46] },  // Fmaj7
      { bass:[58.27,87.31],  notes:[233.08,293.66,349.23,466.16,587.33] },  // Bb7
      { bass:[55.00,82.41],  notes:[220.00,261.63,329.63,392.00,440.00] },  // Am7
      { bass:[73.42,110.00], notes:[293.66,349.23,440.00,523.25,587.33] },  // Dm7
    ],
    C: [
      { bass:[55.00,82.41],  notes:[220.00,261.63,329.63,392.00] },         // Am7 sparse
      { bass:[73.42,110.00], notes:[293.66,349.23,440.00,523.25] },         // Dm7
      { bass:[49.00,73.42],  notes:[392.00,493.88,587.33,659.26] },         // G7
      { bass:[65.41,98.00],  notes:[261.63,329.63,392.00,493.88] },         // Cmaj7
      { bass:[55.00,82.41],  notes:[220.00,261.63,329.63,392.00] },
      { bass:[82.41,123.47], notes:[329.63,392.00,493.88,587.33] },         // Em7
      { bass:[87.31,130.81], notes:[349.23,440.00,523.25,659.26] },         // Fmaj7
      { bass:[49.00,73.42],  notes:[392.00,493.88,587.33,783.99] },         // G7
    ],
  };

  // Melody: [scale_index, beat_offset, duration_beats, vol_mult]
  // Scale: C D E F G A B C D E (0-9)
  const SCALE = [261.63,293.66,329.63,349.23,392.00,440.00,493.88,523.25,587.33,659.26,698.46,783.99];

  const MELODIES = {
    A: [
      [7,0,0.5,1.0],[5,0.75,0.5,0.7],[7,1.5,1.0,0.9],[5,3.0,0.5,0.6],[4,3.5,0.5,0.5],
      [4,0,1.0,0.8],[2,1.5,0.5,0.6],[4,2.5,0.5,0.7],[5,3.5,0.5,0.5],
      [7,0,0.5,1.0],[8,0.75,0.5,0.8],[7,1.5,0.5,0.9],[5,2.5,1.0,0.7],[4,3.75,0.25,0.5],
      [5,0,1.5,0.8],[4,2.0,0.5,0.6],[2,3.0,1.0,0.7],
    ],
    B: [
      [8,0,0.5,1.0],[7,0.75,0.5,0.8],[8,1.5,1.0,1.0],[7,3.0,0.5,0.7],[5,3.5,0.5,0.6],
      [5,0,1.0,0.9],[4,1.5,0.5,0.7],[5,2.5,0.5,0.8],[7,3.5,0.5,0.6],
      [10,0,0.5,1.0],[8,0.75,0.5,0.8],[7,1.5,0.5,0.9],[5,2.5,0.5,0.7],[4,3.0,1.0,0.6],
      [5,0,2.0,0.9],[4,2.5,0.5,0.6],[2,3.5,0.5,0.5],
    ],
    C: [
      [4,0,1.0,0.6],[7,2.5,1.0,0.5],
      [5,1.0,1.5,0.6],[4,3.5,0.5,0.4],
      [7,0,0.5,0.7],[5,2.0,2.0,0.5],
      [2,0,2.0,0.5],[4,3.0,1.0,0.4],
    ],
  };

  // Section map: 8 bars A, 8 bars B, 4 bars C, repeat
  const SECTION_MAP = [
    ...'AAAAAAAA'.split(''),
    ...'BBBBBBBB'.split(''),
    ...'CCCC'.split(''),
  ];

  function getSection(bar) { return SECTION_MAP[bar % SECTION_MAP.length]; }
  function getChord(bar) {
    const sec = getSection(bar);
    const arr = CHORDS[sec];
    return arr[bar % arr.length];
  }

  // Synthesised reverb impulse
  function buildReverb() {
    const len = ctx.sampleRate * 2.2;
    const buf = ctx.createBuffer(2, len, ctx.sampleRate);
    for (let c=0; c<2; c++) {
      const d = buf.getChannelData(c);
      for (let i=0; i<len; i++) d[i] = (Math.random()*2-1) * Math.pow(1-i/len, 2.5);
    }
    const conv = ctx.createConvolver();
    conv.buffer = buf;
    return conv;
  }

  function createCtx() {
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    masterGain = ctx.createGain();
    masterGain.gain.value = muted ? 0 : vol;
    reverbNode = buildReverb();
    dryGain = ctx.createGain(); dryGain.gain.value = 0.78;
    wetGain = ctx.createGain(); wetGain.gain.value = 0.22;
    reverbNode.connect(wetGain);
    wetGain.connect(masterGain);
    dryGain.connect(masterGain);
    masterGain.connect(ctx.destination);
  }

  function playNote(freq, t, dur, gain, type='sine', reverb=true) {
    const osc = ctx.createOscillator();
    const g   = ctx.createGain();
    osc.type  = type;
    osc.frequency.setValueAtTime(freq, t);
    osc.detune.setValueAtTime((Math.random()-0.5)*5, t);
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(gain, t+0.03);
    g.gain.exponentialRampToValueAtTime(0.001, t+dur*0.9);
    osc.connect(g);
    g.connect(reverb ? reverbNode : dryGain);
    g.connect(dryGain);
    osc.start(t); osc.stop(t+dur+0.1);
    scheduledNodes.push(osc);
  }

  function playNoise(t, dur, gain, hi, lo) {
    const buf  = ctx.createBuffer(1, Math.ceil(ctx.sampleRate*dur), ctx.sampleRate);
    const data = buf.getChannelData(0);
    for (let i=0; i<data.length; i++) data[i] = Math.random()*2-1;
    const src = ctx.createBufferSource();
    const hp  = ctx.createBiquadFilter(); hp.type='highpass'; hp.frequency.value=hi;
    const lp  = ctx.createBiquadFilter(); lp.type='lowpass';  lp.frequency.value=lo;
    const g   = ctx.createGain();
    g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.001, t+dur);
    src.buffer=buf; src.connect(hp); hp.connect(lp); lp.connect(g); g.connect(dryGain);
    src.start(t); src.stop(t+dur+0.01);
    scheduledNodes.push(src);
  }

  function strumChord(chord, t, section) {
    const sparse = section === 'C';
    const hits   = sparse ? [0, 2.0] : [0, 0.75, 1.5, 2.5, 3.0, 3.5];
    const count  = sparse ? 3 : chord.notes.length;
    chord.notes.slice(0,count).forEach((freq, i) => {
      hits.forEach(beat => {
        playNote(freq, t+beat*BEAT+i*0.013, BEAT*0.5, (sparse?0.022:0.035)+(i===0?0.012:0), 'triangle', true);
      });
    });
  }

  function walkBass(chord, t, section) {
    const [root, fifth] = chord.bass;
    if (section === 'C') {
      playNote(root,  t,           BEAT*0.9, 0.18, 'sine', false);
      playNote(fifth, t+BEAT*2,   BEAT*0.7, 0.12, 'sine', false);
    } else {
      // Walking: root → chromatic passing → fifth → approach
      [root, root*1.059, fifth, fifth*1.059].forEach((freq, i) => {
        playNote(freq, t+i*BEAT, BEAT*0.85, 0.16-i*0.02, 'sine', false);
      });
    }
  }

  function playRhythm(t, section) {
    const sparse = section === 'C';
    // Hi-hat eighth notes
    for (let i=0; i<8; i++) playNoise(t+i*BEAT*0.5, 0.04, sparse?0.012:0.020, 7000, 16000);
    // Clave rimshot
    const claveVol  = sparse ? 0.055 : 0.085;
    const claveHits = sparse ? [2.0, 3.5] : [1.5, 2.0, 3.0, 3.5];
    claveHits.forEach(beat => {
      playNoise(t+beat*BEAT, 0.06, claveVol, 200, 2500);
      playNote(175, t+beat*BEAT, 0.07, 0.035, 'sine', false);
    });
    // Kick on 1 and 3
    if (!sparse) {
      [0, 2.0].forEach(beat => {
        playNote(80, t+beat*BEAT,      0.12, 0.055, 'sine', false);
        playNote(55, t+beat*BEAT+0.04, 0.10, 0.035, 'sine', false);
      });
    }
  }

  function playMelody(t, bar, section) {
    const mels = MELODIES[section];
    const notesPerBar = Math.ceil(mels.length / 4);
    const b    = bar % 4;
    // Skip melody on C section unless it's bar 0 or 2 of the section
    if (section === 'C' && b % 2 !== 0) return;
    mels.slice(b*notesPerBar, (b+1)*notesPerBar).forEach(([deg, beatOff, dur, volMult]) => {
      const freq = SCALE[Math.min(deg, SCALE.length-1)] * (section === 'C' ? 0.5 : 1.0);
      const nt   = t + beatOff*BEAT;
      playNote(freq, nt,      dur*BEAT*0.82, 0.065*volMult, 'triangle', true);
      playNote(freq, nt+0.01, dur*BEAT*0.65, 0.022*volMult, 'sine',     true);
    });
  }

  function scheduleBar(t, bar) {
    const sec   = getSection(bar);
    const chord = getChord(bar);
    strumChord(chord, t, sec);
    walkBass(chord, t, sec);
    playRhythm(t, sec);
    playMelody(t, bar, sec);
  }

  function scheduler() {
    if (!playing || !ctx) return;
    const now  = ctx.currentTime;
    const next = barCount * BAR;
    if (next < now + 2.5) {
      scheduleBar(Math.max(now+0.05, next), barCount);
      barCount++;
    }
    lookahead = setTimeout(scheduler, 150);
  }

  function start() {
    if (!ctx) createCtx();
    if (ctx.state === 'suspended') ctx.resume();
    playing = true; barCount = 0;
    scheduler(); updateIcon();
  }

  function stop() {
    playing = false; clearTimeout(lookahead);
    scheduledNodes.forEach(n => { try { n.stop(); } catch(e){} });
    scheduledNodes = []; updateIcon();
  }

  function updateIcon() {
    const el = document.getElementById('fm-icon');
    if (el) el.classList.toggle('paused', !playing);
  }

  function buildUI() {
    const player = document.createElement('div');
    player.id = 'farm-music';
    player.innerHTML = `
      <div id="fm-icon" title="play/pause">♪</div>
      <span id="fm-section" style="font-size:9px;font-family:monospace;color:rgba(251,191,36,0.35);letter-spacing:0.12em;min-width:10px">—</span>
      <input id="fm-vol" type="range" min="0" max="1" step="0.01" value="${vol}" title="volume" />
      <div id="fm-mute" title="mute">${muted ? '🔇' : '🔊'}</div>
    `;
    const style = document.createElement('style');
    style.textContent = `
      #farm-music { position:fixed;bottom:18px;right:18px;z-index:9997;display:flex;align-items:center;gap:8px;background:rgba(0,0,0,0.55);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:6px 12px;backdrop-filter:blur(10px);font-size:13px;opacity:0.45;transition:opacity 0.3s;user-select:none; }
      #farm-music:hover { opacity:1; }
      #fm-icon { color:#fbbf24;font-size:15px;cursor:pointer;animation:fm-pulse 2.5s ease-in-out infinite; }
      @keyframes fm-pulse { 0%,100%{opacity:0.5;transform:scale(1)} 50%{opacity:1;transform:scale(1.2)} }
      #fm-icon.paused { animation:none;opacity:0.25; }
      #fm-vol { width:55px;accent-color:#fbbf24;cursor:pointer; }
      #fm-mute { cursor:pointer;font-size:13px; }
    `;
    document.head.appendChild(style);
    document.body.appendChild(player);

    setInterval(() => {
      const el = document.getElementById('fm-section');
      if (el && playing) el.textContent = getSection(barCount);
    }, 500);

    document.getElementById('fm-icon').addEventListener('click', () => playing ? stop() : start());
    document.getElementById('fm-vol').addEventListener('input', function() {
      vol = parseFloat(this.value);
      if (masterGain && !muted) masterGain.gain.value = vol;
      localStorage.setItem(KEY_VOL, vol);
    });
    document.getElementById('fm-mute').addEventListener('click', function() {
      muted = !muted;
      if (masterGain) masterGain.gain.value = muted ? 0 : vol;
      this.textContent = muted ? '🔇' : '🔊';
      localStorage.setItem(KEY_MUTE, muted ? '1' : '0');
    });

    const autoStart = () => {
      if (!playing) start();
      document.removeEventListener('click', autoStart);
      document.removeEventListener('keydown', autoStart);
    };
    document.addEventListener('click', autoStart);
    document.addEventListener('keydown', autoStart);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', buildUI);
  else buildUI();
})();
