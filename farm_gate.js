/**
 * JACOB'S MONEY FARM — PASSWORD GATE
 * Include this script at the top of every page's <body>
 * Password: kickrocks!
 * Session: 15 minutes
 */
(function() {
  const PASS     = 'kickrocks!';
  const TIMEOUT  = 10 * 60 * 1000; // 10 minutes
  const KEY_AUTH = 'farm_auth';
  const KEY_TIME = 'farm_auth_time';

  function isAuthed() {
    const auth = localStorage.getItem(KEY_AUTH);
    const t    = parseInt(localStorage.getItem(KEY_TIME) || '0');
    return auth === '1' && (Date.now() - t) < TIMEOUT;
  }

  function setAuthed() {
    localStorage.setItem(KEY_AUTH, '1');
    localStorage.setItem(KEY_TIME, Date.now().toString());
  }

  function showGate() {
    // Build overlay
    const overlay = document.createElement('div');
    overlay.id = 'farm-gate';
    overlay.innerHTML = `
      <canvas id="gate-stars"></canvas>
      <div class="gate-terminal">
        <div class="gate-blink-line"></div>
        <pre class="gate-ascii">
   ___  ___  ___  ___ 
  | __|/ _ \\| _ \\|  |
  |__ \\ (_) |   /|    |
  |___/\\___/|_|_\\|_||_|
        </pre>
        <div class="gate-title">JACOB'S MONEY FARM</div>
        <div class="gate-sub">// secure terminal — identify yourself</div>
        <div class="gate-prompt">
          <span class="gate-cursor-label">$ password:&nbsp;</span>
          <input id="gate-input" type="password" autocomplete="off" spellcheck="false" autofocus />
        </div>
        <div class="gate-msg" id="gate-msg"></div>
        <div class="gate-hint">[ press ENTER to authenticate ]</div>
      </div>
    `;

    // Styles
    const style = document.createElement('style');
    style.textContent = `
      #farm-gate {
        position: fixed; inset: 0; z-index: 99999;
        background: #000;
        display: flex; align-items: center; justify-content: center;
        font-family: 'Courier New', Courier, monospace;
      }
      #gate-stars {
        position: absolute; inset: 0; width: 100%; height: 100%;
      }
      .gate-terminal {
        position: relative; z-index: 2;
        background: rgba(0,0,0,0.75);
        border: 1px solid #1a4a1a;
        box-shadow: 0 0 40px rgba(0,255,70,0.08), inset 0 0 80px rgba(0,0,0,0.5);
        padding: 40px 50px 36px;
        min-width: 360px; max-width: 480px;
        width: 90vw;
        border-radius: 2px;
      }
      .gate-blink-line {
        width: 100%; height: 1px;
        background: linear-gradient(90deg, transparent, #0f0, transparent);
        margin-bottom: 24px;
        animation: scan 3s linear infinite;
      }
      @keyframes scan {
        0%   { opacity: 0; transform: scaleX(0); }
        50%  { opacity: 1; transform: scaleX(1); }
        100% { opacity: 0; transform: scaleX(0); }
      }
      .gate-ascii {
        color: #0f0; font-size: 11px; line-height: 1.3;
        margin: 0 0 16px; text-align: center;
        opacity: 0.6;
      }
      .gate-title {
        color: #0f0; font-size: 18px; font-weight: bold;
        letter-spacing: 4px; text-align: center;
        margin-bottom: 6px;
        text-shadow: 0 0 10px rgba(0,255,70,0.5);
      }
      .gate-sub {
        color: #0a0; font-size: 11px; text-align: center;
        letter-spacing: 2px; margin-bottom: 32px;
      }
      .gate-prompt {
        display: flex; align-items: center;
        border-bottom: 1px solid #0a0;
        padding-bottom: 8px; margin-bottom: 12px;
      }
      .gate-cursor-label { color: #0f0; font-size: 14px; white-space: nowrap; }
      #gate-input {
        background: transparent; border: none; outline: none;
        color: #0f0; font-family: 'Courier New', monospace;
        font-size: 14px; flex: 1; caret-color: #0f0;
        letter-spacing: 2px;
      }
      .gate-msg {
        font-size: 12px; min-height: 18px;
        margin-bottom: 8px; letter-spacing: 1px;
        transition: all 0.3s;
      }
      .gate-msg.err  { color: #f44; }
      .gate-msg.ok   { color: #0f0; }
      .gate-hint {
        color: #040; font-size: 10px; letter-spacing: 2px;
        text-align: center; margin-top: 8px;
      }
      @keyframes shake {
        0%,100% { transform: translateX(0); }
        20%,60% { transform: translateX(-8px); }
        40%,80% { transform: translateX(8px); }
      }
      .gate-shake { animation: shake 0.4s ease; }
    `;

    document.head.appendChild(style);
    document.body.appendChild(overlay);

    // Starfield
    const canvas = document.getElementById('gate-stars');
    const ctx    = canvas.getContext('2d');
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;

    const stars = Array.from({length: 200}, () => ({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      r: Math.random() * 1.5,
      o: Math.random(),
      speed: 0.002 + Math.random() * 0.003,
    }));

    function drawStars() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      stars.forEach(s => {
        s.o += s.speed;
        const opacity = 0.3 + Math.abs(Math.sin(s.o)) * 0.7;
        ctx.beginPath();
        ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
        ctx.fillStyle = `rgba(${150 + Math.floor(Math.random()*50)},${200 + Math.floor(Math.random()*55)},${150 + Math.floor(Math.random()*50)},${opacity})`;
        ctx.fill();
      });
      requestAnimationFrame(drawStars);
    }
    drawStars();

    // Input handler
    const input = document.getElementById('gate-input');
    const msg   = document.getElementById('gate-msg');
    input.focus();

    input.addEventListener('keydown', function(e) {
      if (e.key !== 'Enter') return;
      const val = input.value.trim();
      if (val === PASS) {
        msg.className = 'gate-msg ok';
        msg.textContent = '// access granted — welcome back jacob';
        setAuthed();
        setTimeout(() => overlay.remove(), 600);
      } else {
        msg.className = 'gate-msg err';
        msg.textContent = '// access denied — wrong password';
        input.value = '';
        overlay.querySelector('.gate-terminal').classList.remove('gate-shake');
        void overlay.querySelector('.gate-terminal').offsetWidth;
        overlay.querySelector('.gate-terminal').classList.add('gate-shake');
      }
    });
  }

  // Re-auth check every 30 seconds
  setInterval(function() {
    if (!isAuthed()) showGate();
  }, 30000);

  if (!isAuthed()) showGate();
})();
