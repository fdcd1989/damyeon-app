/* 마스코트: 페이지가 열리면 한 번 인사(greet), 저장에 성공하면 반응(cheer), 클릭하면 다시 인사.
   - 영상 하나를 구간(초)으로 나눠 재생한다. 소리 없음(muted).
   - 'OS 동작 줄이기'를 켠 사용자, 영상 로딩/재생 실패, 탭이 숨겨진 경우에는 정지 그림만 보여준다. */
(function () {
  var el = document.getElementById('mascot');
  if (!el) return;
  var video = el.querySelector('video');
  var poster = el.querySelector('img');
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var SEG = { greet: [0.0, 4.0], cheer: [7.2, 9.8] };
  var REST = '/static/mascot/rest.webp', HAPPY = '/static/mascot/happy.webp';
  var ready = false, token = 0, rafId = null, restTimer = null;

  function setPlaying(on) { el.classList.toggle('is-playing', on); }
  function stop() {
    token++;
    if (rafId) cancelAnimationFrame(rafId);
    clearTimeout(restTimer);
    try { video.pause(); } catch (e) {}
    setPlaying(false);
  }
  function still(name) {            // 영상을 쓸 수 없을 때: 정지 그림으로만 반응
    if (name !== 'cheer') return;
    poster.src = HAPPY;
    clearTimeout(restTimer);
    restTimer = setTimeout(function () { poster.src = REST; }, 2500);
  }
  function play(name) {
    stop();
    if (reduce || !ready) { still(name); return; }
    var seg = SEG[name], my = token;
    try { video.currentTime = seg[0]; } catch (e) { still(name); return; }
    var p = video.play();
    var begin = function () {
      if (my !== token) return;
      setPlaying(true);
      (function tick() {
        if (my !== token) return;
        if (video.currentTime >= seg[1] || video.ended) {
          video.pause();
          restTimer = setTimeout(function () { if (my === token) setPlaying(false); }, name === 'cheer' ? 700 : 150);
          return;
        }
        rafId = requestAnimationFrame(tick);
      })();
    };
    if (p && p.then) p.then(begin).catch(function () { setPlaying(false); still(name); });
    else begin();
  }

  var started = false;
  function onReady() {
    if (ready) return;
    ready = true;
    if (!started) { started = true; play('greet'); }
  }
  video.addEventListener('canplay', onReady);
  video.addEventListener('error', function () { ready = false; setPlaying(false); });
  if (video.readyState >= 3) onReady();
  setTimeout(function () { if (!started) { started = true; /* 로딩이 늦으면 정지 그림만 유지 */ } }, 4000);

  el.addEventListener('click', function () { play('greet'); });
  document.addEventListener('visibilitychange', function () { if (document.hidden) stop(); });


  /* ---------- 위치: 평가 화면에서는 두 '자리' 사이를 오간다 ----------
     home  : 제목 줄 오른쪽 큰 자리 (페이지와 함께 스크롤)
     dock  : 사이드바 제목 옆 작은 자리 (사이드바는 화면에 고정이라 항상 보임). 큰 자리가 화면 위로 지나가면 이쪽으로 이동
     mobile: 좁은 화면(≤860px)은 사이드바가 위에 쌓이므로 사이드바 제목 옆에 작게 둔다 */
  var mainSlot = document.getElementById('mascot-slot-main');
  var sideSlot = document.getElementById('mascot-slot-side');
  if (el.classList.contains('mascot--eval') && sideSlot) {
    var desktopMQ = window.matchMedia('(min-width: 861px)');
    var state = null, moving = false, moveTimer = null, ticking = false;
    var DOCK_BELOW = 50, HOME_ABOVE = 130;     // 이동 기준(히스테리시스: 경계에서 깜빡이지 않게)

    var rectOf = function (slot) { return slot.getBoundingClientRect(); };
    var put = function (mode, r) {
      var st = el.style;
      st.position = mode;
      st.left = (mode === 'fixed' ? r.left : r.left + window.pageXOffset) + 'px';
      st.top = (mode === 'fixed' ? r.top : r.top + window.pageYOffset) + 'px';
      st.width = r.width + 'px';
    };
    var placed = function () { el.classList.add('is-placed'); };
    var endMove = function (after) {
      clearTimeout(moveTimer);
      moveTimer = setTimeout(function () { moving = false; el.classList.remove('is-gliding'); if (after) after(); }, reduce ? 0 : 420);
    };

    function glideTo(target, mode, settle) {          // 현재 보이는 자리에서 target 자리로 부드럽게 이동
      var cur = el.getBoundingClientRect();
      moving = true;
      el.classList.remove('is-gliding');
      put('fixed', cur);                              // 지금 모습 그대로 fixed로 전환(점프 없음)
      void el.offsetWidth;
      if (!reduce) el.classList.add('is-gliding');
      put('fixed', target);
      endMove(settle);
    }
    function layout(animate) {
      if (!desktopMQ.matches) {                       // 모바일/좁은 화면
        state = 'mobile'; moving = false; el.classList.remove('is-gliding'); put('absolute', rectOf(sideSlot)); placed(); return;
      }
      var home = mainSlot && rectOf(mainSlot);
      var wantDock = !home || home.bottom < DOCK_BELOW;
      if (state === 'mobile' || state === null || !animate) {
        state = wantDock ? 'dock' : 'home';
        moving = false; el.classList.remove('is-gliding');
        if (state === 'dock') put('fixed', rectOf(sideSlot)); else put('absolute', home);
        placed(); return;
      }
      if (moving) return;
      if (state === 'home' && wantDock) {
        state = 'dock'; glideTo(rectOf(sideSlot), 'fixed', null);
      } else if (state === 'dock' && home && home.bottom > HOME_ABOVE) {
        state = 'home';
        glideTo(rectOf(mainSlot), 'fixed', function () { put('absolute', rectOf(mainSlot)); });
      } else if (!moving) {                           // 같은 자리 유지: 좌표만 갱신(창 크기 변화 등)
        if (state === 'dock') put('fixed', rectOf(sideSlot)); else put('absolute', home);
      }
    }
    var onScroll = function () {
      if (ticking) return; ticking = true;
      requestAnimationFrame(function () { ticking = false; layout(true); });
    };
    var relayout = function () { layout(false); };
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', relayout);
    window.addEventListener('load', relayout);
    if (desktopMQ.addEventListener) desktopMQ.addEventListener('change', relayout);
    if (window.ResizeObserver) new ResizeObserver(function () { if (!moving) relayout(); }).observe(document.body);
    relayout();
  }

  window.Mascot = { greet: function () { play('greet'); }, cheer: function () { play('cheer'); } };
})();
