'use client';

import React, { useState } from 'react';
import { FiLock, FiEye, FiEyeOff, FiArrowRight, FiUserPlus, FiShield } from 'react-icons/fi';
import { loginWithPassword, setupAccount } from '../lib/api';

const inputClass =
  'w-full bg-[#222226] border border-[#2e2e34] rounded-xl px-3.5 py-2.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

function PasswordField({ value, onChange, placeholder, autoComplete, autoFocus }) {
  const [show, setShow] = useState(false);
  return (
    <div className="relative">
      <input
        suppressHydrationWarning={true}
        type={show ? 'text' : 'password'}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        placeholder={placeholder}
        className={`${inputClass} pr-9`}
      />
      <button
        suppressHydrationWarning={true}
        type="button"
        onClick={() => setShow(!show)}
        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-zinc-400 hover:text-white transition text-xs"
        title={show ? 'Hide' : 'Show'}
      >
        {show ? <FiEyeOff /> : <FiEye />}
      </button>
    </div>
  );
}

// The sign-in page, or on a fresh install the setup page that creates the
// owner's account. With two-factor on, the code is asked for after the
// password is accepted.
export default function LoginScreen({ setupRequired = false, onAuthenticated }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [code, setCode] = useState('');
  const [needsCode, setNeedsCode] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (busy) return;
    setError('');
    if (setupRequired && password !== confirm) {
      setError('The passwords do not match.');
      return;
    }
    setBusy(true);
    try {
      let session;
      if (setupRequired) {
        session = await setupAccount(username.trim(), password);
      } else {
        session = await loginWithPassword(username.trim(), password, needsCode ? code.trim() : null);
      }
      setPassword('');
      setConfirm('');
      setCode('');
      if (onAuthenticated) onAuthenticated(session);
    } catch (err) {
      if (err?.reason === 'totp_required') {
        setNeedsCode(true);
      } else {
        setError(err?.message || 'Sign in failed.');
      }
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = username.trim() && password && (!setupRequired || confirm) && (!needsCode || code.trim());

  return (
    <div className="h-screen w-screen flex items-center justify-center bg-[#09090b] text-zinc-100 font-sans select-none p-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-[#111113] border border-[#1c1c20] rounded-2xl shadow-2xl p-6 space-y-5 animate-fade-in"
      >
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-[#18181b] border border-[#27272a] text-amber-400 flex items-center justify-center text-lg">
            {setupRequired ? <FiUserPlus /> : needsCode ? <FiShield /> : <FiLock />}
          </div>
          <div>
            <h1 className="text-sm font-bold tracking-wide">Cubicle</h1>
            <p className="text-xs text-zinc-400 mt-0.5">
              {setupRequired
                ? 'Create the owner account for this server.'
                : needsCode
                  ? 'Enter the code from your authenticator app.'
                  : 'Sign in to continue.'}
            </p>
          </div>
        </div>

        {!needsCode && (
          <>
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300">Username</label>
              <input
                suppressHydrationWarning={true}
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                autoComplete="username"
                placeholder={setupRequired ? 'Pick a username' : 'Username'}
                className={inputClass}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-xs font-semibold text-zinc-300">Password</label>
              <PasswordField
                value={password}
                onChange={setPassword}
                autoComplete={setupRequired ? 'new-password' : 'current-password'}
                placeholder={setupRequired ? 'At least 12 characters' : 'Password'}
              />
            </div>
            {setupRequired && (
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-zinc-300">Confirm password</label>
                <PasswordField value={confirm} onChange={setConfirm} autoComplete="new-password" placeholder="Type it again" />
              </div>
            )}
          </>
        )}

        {needsCode && (
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-zinc-300">Authenticator code</label>
            <input
              suppressHydrationWarning={true}
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
              placeholder="123456, or a recovery code"
              className={`${inputClass} font-mono tracking-widest`}
            />
            <button type="button" onClick={() => { setNeedsCode(false); setCode(''); }} className="text-[10px] text-zinc-500 hover:text-zinc-300 underline-offset-2 hover:underline">
              Back
            </button>
          </div>
        )}

        {error && (
          <p className="text-[11px] text-red-300 bg-red-500/10 border border-red-500/20 rounded-xl px-3 py-2">{error}</p>
        )}

        <button
          suppressHydrationWarning={true}
          type="submit"
          disabled={busy || !canSubmit}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 rounded-xl bg-zinc-100 text-zinc-900 text-xs font-semibold hover:bg-white transition disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span>{busy ? 'Working…' : setupRequired ? 'Create account' : needsCode ? 'Verify' : 'Sign in'}</span>
          <FiArrowRight className="text-sm" />
        </button>

        {setupRequired && (
          <p className="text-[10px] leading-relaxed text-zinc-500">
            Everything this server already holds belongs to this account. You can switch on two-factor authentication and create API tokens afterwards in App Settings → Account.
          </p>
        )}
      </form>
    </div>
  );
}
