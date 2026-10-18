'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiShield, FiKey, FiTrash2, FiPlus, FiCopy, FiLogOut, FiCheck } from 'react-icons/fi';
import {
  fetchAccount,
  changePassword,
  startTotp,
  confirmTotp,
  disableTotp,
  createApiToken,
  deleteApiToken,
  signOutOtherSessions,
} from '../lib/api';

const inputClass =
  'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';
const buttonClass =
  'px-3 py-2 rounded-xl text-xs font-medium bg-[#1f1f23] text-zinc-200 hover:bg-[#27272b] disabled:opacity-40 transition flex items-center gap-1.5';

function when(value) {
  if (!value) return 'never';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function Copyable({ value }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      /* the text is selectable below */
    }
  };
  return (
    <div className="flex items-center gap-2">
      <code className="flex-1 text-[11px] font-mono text-zinc-100 bg-[#0f0f12] border border-[#2b2b32] rounded-lg px-2.5 py-1.5 break-all select-text">{value}</code>
      <button type="button" onClick={copy} className={buttonClass} title="Copy">
        {copied ? <FiCheck className="text-emerald-400" /> : <FiCopy />}
      </button>
    </div>
  );
}

// The signed-in account: password, two-factor, API tokens, other sessions.
export default function AccountSection({ onSignOut }) {
  const [account, setAccount] = useState(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const [enrolment, setEnrolment] = useState(null); // { otpauth_url, secret, qr_svg }
  const [totpCode, setTotpCode] = useState('');
  const [recoveryCodes, setRecoveryCodes] = useState(null);
  const [disablePassword, setDisablePassword] = useState('');

  const [tokenName, setTokenName] = useState('');
  const [newToken, setNewToken] = useState(null); // { name, token }

  const refresh = useCallback(async () => {
    try {
      setAccount(await fetchAccount());
    } catch (err) {
      setError(err?.message || 'Could not load the account.');
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (action, failure) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await action();
      await refresh();
      return result;
    } catch (err) {
      setError(err?.message || failure);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const submitPassword = async (e) => {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      setError('The new passwords do not match.');
      return;
    }
    const ok = await run(() => changePassword(currentPassword, newPassword), 'Could not change the password.');
    if (ok) {
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setNotice('Password changed. Other devices have been signed out.');
    }
  };

  const beginTotp = async () => {
    const result = await run(() => startTotp(), 'Could not start two-factor setup.');
    if (result) {
      setEnrolment(result);
      setRecoveryCodes(null);
      setTotpCode('');
    }
  };

  const finishTotp = async (e) => {
    e.preventDefault();
    const result = await run(() => confirmTotp(totpCode.trim()), 'That code was not accepted.');
    if (result) {
      setEnrolment(null);
      setTotpCode('');
      setRecoveryCodes(result.recovery_codes || []);
    }
  };

  const endTotp = async (e) => {
    e.preventDefault();
    const ok = await run(() => disableTotp(disablePassword), 'Could not switch off two-factor.');
    if (ok) {
      setDisablePassword('');
      setRecoveryCodes(null);
      setNotice('Two-factor authentication is off.');
    }
  };

  const addToken = async (e) => {
    e.preventDefault();
    const result = await run(() => createApiToken(tokenName.trim() || 'token'), 'Could not create the token.');
    if (result) {
      setNewToken({ name: result.name, token: result.token });
      setTokenName('');
    }
  };

  if (!account) {
    return <p className="text-[11px] text-zinc-500">{error || 'Loading…'}</p>;
  }

  const otherSessions = (account.sessions || []).filter((s) => !s.current).length;

  return (
    <div className="space-y-5">
      <div className="text-xs text-zinc-300">
        Signed in as <span className="font-semibold text-zinc-100">{account.username}</span>
        <span className="text-zinc-500"> · {account.role}</span>
        {account.last_login_at && <span className="text-zinc-500"> · last sign-in {when(account.last_login_at)}</span>}
      </div>

      {/* Password */}
      <form onSubmit={submitPassword} className="space-y-2">
        <h4 className="text-[11px] font-semibold text-zinc-200 flex items-center gap-1.5"><FiKey /> Password</h4>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <input type="password" autoComplete="current-password" value={currentPassword} onChange={(e) => setCurrentPassword(e.target.value)} placeholder="Current password" className={inputClass} />
          <input type="password" autoComplete="new-password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="New (12+ characters)" className={inputClass} />
          <input type="password" autoComplete="new-password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} placeholder="Confirm new" className={inputClass} />
        </div>
        <button type="submit" disabled={busy || !currentPassword || !newPassword || !confirmPassword} className={buttonClass}>
          Change password
        </button>
      </form>

      {/* Two-factor */}
      <div className="space-y-2">
        <h4 className="text-[11px] font-semibold text-zinc-200 flex items-center gap-1.5"><FiShield /> Two-factor authentication</h4>
        {account.totp_enabled ? (
          <form onSubmit={endTotp} className="space-y-2">
            <p className="text-[11px] text-emerald-300">
              On. Sign-in asks for a code from your authenticator app.
              {typeof account.recovery_codes_left === 'number' && <span className="text-zinc-500"> {account.recovery_codes_left} recovery codes left.</span>}
            </p>
            <div className="flex items-center gap-2">
              <input type="password" autoComplete="current-password" value={disablePassword} onChange={(e) => setDisablePassword(e.target.value)} placeholder="Password to switch off" className={`${inputClass} flex-1`} />
              <button type="submit" disabled={busy || !disablePassword} className={buttonClass}>Switch off</button>
            </div>
          </form>
        ) : enrolment ? (
          <form onSubmit={finishTotp} className="space-y-3">
            <p className="text-[11px] text-zinc-400">Scan this with your authenticator app (or enter the key by hand), then type the code it shows.</p>
            <div className="flex flex-col sm:flex-row gap-3 items-start">
              {enrolment.qr_svg && <img src={enrolment.qr_svg} alt="Two-factor QR code" className="w-40 h-40 rounded-lg bg-white p-1" />}
              <div className="space-y-2 flex-1 min-w-0">
                <div className="text-[10px] text-zinc-500">Key</div>
                <Copyable value={enrolment.secret} />
                <div className="flex items-center gap-2">
                  <input type="text" inputMode="numeric" autoComplete="one-time-code" value={totpCode} onChange={(e) => setTotpCode(e.target.value)} placeholder="Code from the app" className={`${inputClass} font-mono tracking-widest flex-1`} />
                  <button type="submit" disabled={busy || !totpCode.trim()} className={buttonClass}>Turn on</button>
                  <button type="button" onClick={() => setEnrolment(null)} className="text-[11px] text-zinc-500 hover:text-zinc-200">Cancel</button>
                </div>
              </div>
            </div>
          </form>
        ) : (
          <div className="space-y-2">
            <p className="text-[11px] text-zinc-500">Off. Add a code from an authenticator app to every sign-in.</p>
            <button type="button" onClick={beginTotp} disabled={busy} className={buttonClass}><FiShield /> Set up two-factor</button>
          </div>
        )}
        {recoveryCodes && (
          <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 space-y-2">
            <p className="text-[11px] text-amber-200">Two-factor is on. Save these recovery codes somewhere safe; each one signs you in once if you lose the app. They are not shown again.</p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
              {recoveryCodes.map((code) => (
                <code key={code} className="text-[11px] font-mono text-zinc-100 bg-[#0f0f12] border border-[#2b2b32] rounded-lg px-2 py-1 text-center select-text">{code}</code>
              ))}
            </div>
            <button type="button" onClick={() => setRecoveryCodes(null)} className="text-[11px] text-zinc-500 hover:text-zinc-200">I have saved them</button>
          </div>
        )}
      </div>

      {/* API tokens */}
      <div className="space-y-2">
        <h4 className="text-[11px] font-semibold text-zinc-200 flex items-center gap-1.5"><FiKey /> API tokens</h4>
        <p className="text-[11px] text-zinc-500">For scripts and integrations: send one as <span className="font-mono">Authorization: Bearer …</span>. A token acts as you.</p>
        {(account.tokens || []).length > 0 && (
          <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
            {account.tokens.map((token) => (
              <li key={token.id} className="px-3 py-2 bg-[#18181b] flex items-center gap-3">
                <span className="text-xs text-zinc-100 truncate">{token.name}</span>
                <span className="text-[10px] text-zinc-500 truncate">created {when(token.created_at)} · last used {when(token.last_used_at)}</span>
                <button type="button" disabled={busy} onClick={() => run(() => deleteApiToken(token.id), 'Could not revoke the token.')} className="ml-auto px-2 py-1 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition" title="Revoke">
                  <FiTrash2 className="text-[10px]" />
                </button>
              </li>
            ))}
          </ul>
        )}
        {newToken && (
          <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3 space-y-2">
            <p className="text-[11px] text-emerald-200">Token &quot;{newToken.name}&quot; created. Copy it now; it is not shown again.</p>
            <Copyable value={newToken.token} />
            <button type="button" onClick={() => setNewToken(null)} className="text-[11px] text-zinc-500 hover:text-zinc-200">Done</button>
          </div>
        )}
        <form onSubmit={addToken} className="flex items-center gap-2">
          <input type="text" value={tokenName} onChange={(e) => setTokenName(e.target.value)} placeholder="Name, e.g. home automation" maxLength={80} className={`${inputClass} flex-1`} />
          <button type="submit" disabled={busy} className={buttonClass}><FiPlus /> Create token</button>
        </form>
      </div>

      {/* Sessions */}
      <div className="space-y-2">
        <h4 className="text-[11px] font-semibold text-zinc-200 flex items-center gap-1.5"><FiLogOut /> Sessions</h4>
        <p className="text-[11px] text-zinc-500">
          {otherSessions === 0 ? 'This is the only signed-in device.' : `${otherSessions} other device${otherSessions === 1 ? '' : 's'} signed in.`}
        </p>
        <div className="flex items-center gap-2">
          {otherSessions > 0 && (
            <button type="button" disabled={busy} onClick={() => run(() => signOutOtherSessions(), 'Could not sign out other devices.')} className={buttonClass}>
              Sign out other devices
            </button>
          )}
          <button type="button" onClick={onSignOut} className="text-[11px] text-zinc-500 hover:text-zinc-200 transition underline-offset-2 hover:underline">
            Sign out of this browser
          </button>
        </div>
      </div>

      {notice && <p className="text-[11px] text-emerald-300">{notice}</p>}
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
    </div>
  );
}
