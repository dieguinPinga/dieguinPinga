'use strict';
(function () {
  const err = new URLSearchParams(location.search).get('err');
  if (!err) return;
  const el = document.getElementById('err');
  el.textContent = err === 'bloqueado'
    ? 'Demasiados intentos fallidos. Esperá 15 minutos.'
    : 'Usuario o contraseña incorrectos.';
  el.hidden = false;
})();
