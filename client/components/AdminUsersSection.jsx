'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiUsers, FiUserPlus, FiShield, FiSlash, FiCheckCircle, FiKey } from 'react-icons/fi';
import { fetchAdminUsers, createAdminUser, updateAdminUser } from '../lib/api';

const inputClass =
  'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';
const buttonClass =
  'px-3 py-2 rounded-xl text-xs font-medium bg-[#1f1f23] text-zinc-200 hover:bg-[#27272b] disabled:opacity-40 transition flex items-center gap-1.5';

function when(value) {
  if (!value) return 'never';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

// Accounts on this server, for admins: create, promote, disable, reset.
export default function AdminUsersSection({ currentUser }) {
  const [users, setUsers] = useState([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('member');
  const [resetFor, setResetFor] = useState(null); // user id whose password is being reset
  const [resetPassword, setResetPassword] = useState('');

  const refresh = useCallback(async () => {
    try {
      setUsers((await fetchAdminUsers()).users || []);
    } catch (err) {
      setError(err?.message || 'Could not load the users.');
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const run = async (action, failure, done) => {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const result = await action();
      await refresh();
      if (done) setNotice(done);
      return result;
    } catch (err) {
      setError(err?.message || failure);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const create = async (e) => {
    e.preventDefault();
    const ok = await run(() => createAdminUser(username.trim(), password, role), 'Could not create the account.', `Account ${username.trim()} created.`);
    if (ok) {
      setUsername('');
      setPassword('');
      setRole('member');
    }
  };

  const submitReset = async (e) => {
    e.preventDefault();
    const target = users.find((u) => u.id === resetFor);
    const ok = await run(() => updateAdminUser(resetFor, { password: resetPassword }), 'Could not reset the password.', `Password reset for ${target?.username || 'the user'}; they are signed out everywhere.`);
    if (ok) {
      setResetFor(null);
      setResetPassword('');
    }
  };

  const isSelf = (user) => currentUser && user.id === currentUser.id;

  return (
    <div className="space-y-4">
      {users.length > 0 && (
        <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
          {users.map((user) => (
            <li key={user.id} className={`px-3 py-2 ${user.disabled ? 'bg-[#141416]' : 'bg-[#18181b]'}`}>
              <div className="flex items-center gap-3">
                <FiUsers className={`text-xs flex-shrink-0 ${user.disabled ? 'text-zinc-600' : 'text-cyan-300/80'}`} />
                <span className={`text-xs truncate ${user.disabled ? 'text-zinc-500 line-through' : 'text-zinc-100'}`}>{user.username}</span>
                <span className="text-[10px] text-zinc-500">{user.role}{isSelf(user) ? ' · you' : ''}</span>
                {user.totp_enabled && <FiShield className="text-[10px] text-emerald-400" title="Two-factor on" />}
                <span className="ml-auto flex items-center gap-1 flex-shrink-0">
                  <button
                    type="button"
                    disabled={busy || isSelf(user) || user.id === 'local-user'}
                    onClick={() => run(() => updateAdminUser(user.id, { role: user.role === 'owner' ? 'member' : 'owner' }), 'Could not change the role.')}
                    className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-40 transition"
                    title={user.role === 'owner' ? 'Make a member' : 'Make an admin'}
                  >
                    <FiShield className="text-[10px]" />
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => { setResetFor(resetFor === user.id ? null : user.id); setResetPassword(''); }}
                    className="px-2 py-1 rounded-lg text-[11px] text-zinc-300 hover:text-white hover:bg-[#1f1f23] disabled:opacity-40 transition"
                    title="Reset password"
                  >
                    <FiKey className="text-[10px]" />
                  </button>
                  <button
                    type="button"
                    disabled={busy || isSelf(user) || user.id === 'local-user'}
                    onClick={() => run(() => updateAdminUser(user.id, { disabled: !user.disabled }), 'Could not change the account.')}
                    className={`px-2 py-1 rounded-lg text-[11px] hover:bg-[#1f1f23] disabled:opacity-40 transition ${user.disabled ? 'text-emerald-300' : 'text-rose-400'}`}
                    title={user.disabled ? 'Enable' : 'Disable (signs them out)'}
                  >
                    {user.disabled ? <FiCheckCircle className="text-[10px]" /> : <FiSlash className="text-[10px]" />}
                  </button>
                </span>
              </div>
              <div className="pl-6 mt-0.5 text-[10px] text-zinc-500">
                last sign-in {when(user.last_login_at)}{user.disabled ? ' · disabled' : ''}
              </div>
              {resetFor === user.id && (
                <form onSubmit={submitReset} className="pl-6 mt-2 flex items-center gap-2">
                  <input type="password" autoComplete="new-password" value={resetPassword} onChange={(e) => setResetPassword(e.target.value)} placeholder="New password (12+ characters)" className={`${inputClass} flex-1`} />
                  <button type="submit" disabled={busy || resetPassword.length < 12} className={buttonClass}>Set</button>
                  <button type="button" onClick={() => setResetFor(null)} className="text-[11px] text-zinc-500 hover:text-zinc-200">Cancel</button>
                </form>
              )}
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={create} className="space-y-2 rounded-xl border border-[#27272a] p-3 bg-[#141416]">
        <div className="text-[11px] font-semibold text-zinc-200 flex items-center gap-1.5"><FiUserPlus /> New account</div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
          <input type="text" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" className={inputClass} />
          <input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Password (12+ characters)" className={inputClass} />
          <select value={role} onChange={(e) => setRole(e.target.value)} className={inputClass}>
            <option value="member">Member</option>
            <option value="owner">Admin</option>
          </select>
        </div>
        <button type="submit" disabled={busy || !username.trim() || password.length < 12} className={buttonClass}>
          <FiUserPlus /> Create account
        </button>
        <p className="text-[10px] leading-relaxed text-zinc-500">
          Give the person their password to sign in; they can change it and switch on two-factor afterwards in their own Account section. Members
          see their own bots, chats, notes, schedules, secrets and app connections. Admins also manage the model server, the connector key, system
          MCP servers and these accounts.
        </p>
      </form>

      {notice && <p className="text-[11px] text-emerald-300">{notice}</p>}
      {error && <p className="text-[11px] text-rose-400">{error}</p>}
    </div>
  );
}
