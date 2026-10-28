'use client';

import React, { useCallback, useEffect, useState } from 'react';
import { FiUserPlus, FiX } from 'react-icons/fi';
import { addBotMember, changeBotMember, fetchBotMembers, fetchPeople, removeBotMember } from '../lib/api';

const ROLE_HELP = {
  member: 'Chats with the bot and changes its settings, notes and schedules',
  admin: 'Also adds and removes members and can reset the computer',
};

// Who has a bot besides its owner. The owner adds members and bot admins, a
// bot admin adds members; anyone it is shared with can leave it. Each person's
// conversations with the bot are their own.
export default function SharingSection({ bot, onLeave }) {
  const [listing, setListing] = useState(null);
  const [people, setPeople] = useState([]);
  const [username, setUsername] = useState('');
  const [role, setRole] = useState('member');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const refresh = useCallback(async () => {
    try {
      setListing(await fetchBotMembers(bot.id));
    } catch (err) {
      setError(err?.message || 'Could not load who has this bot.');
    }
  }, [bot.id]);

  useEffect(() => {
    refresh();
    fetchPeople().then(setPeople).catch(() => setPeople([]));
  }, [refresh]);

  const run = async (action, failure) => {
    setBusy(true);
    setError('');
    try {
      const result = await action();
      if (result && result.members) setListing(result);
      return true;
    } catch (err) {
      setError(err?.message || failure);
      return false;
    } finally {
      setBusy(false);
    }
  };

  if (!listing) return <p className="text-[11px] text-zinc-500">{error || 'Loading…'}</p>;

  const myRole = listing.you?.role || 'member';
  const manages = myRole === 'owner' || myRole === 'admin';
  const taken = new Set([listing.owner.user_id, ...listing.members.map((m) => m.user_id)]);
  const candidates = people.filter((p) => !taken.has(p.user_id));
  const inputClass =
    'bg-[#222226] border border-[#2e2e34] rounded-xl px-3 py-2 text-xs text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-zinc-400 transition';

  return (
    <div className="space-y-3">
      <ul className="divide-y divide-[#232328] rounded-xl border border-[#27272a] overflow-hidden">
        <li className="px-3 py-2 bg-[#18181b] flex items-center gap-2 text-xs">
          <span className="text-zinc-100 truncate">{listing.owner.name}</span>
          <span className="text-[10px] text-zinc-500 truncate">@{listing.owner.username}</span>
          <span className="ml-auto text-[10px] text-zinc-400">owner</span>
        </li>
        {listing.members.map((member) => {
          const me = member.user_id === listing.you?.user_id;
          const canRemove = !me && (myRole === 'owner' || (myRole === 'admin' && member.role === 'member'));
          return (
            <li key={member.user_id} className="px-3 py-2 bg-[#18181b] flex items-center gap-2 text-xs">
              <span className="text-zinc-100 truncate">{member.name}{me ? ' (you)' : ''}</span>
              <span className="text-[10px] text-zinc-500 truncate">@{member.username}</span>
              <span className="ml-auto flex items-center gap-1">
                {myRole === 'owner' ? (
                  <select
                    value={member.role}
                    disabled={busy}
                    onChange={(e) => run(() => changeBotMember(bot.id, member.user_id, e.target.value), 'Could not change the role.')}
                    className="bg-transparent text-[10px] text-zinc-300 border border-[#2e2e34] rounded-md px-1 py-0.5"
                    title={ROLE_HELP[member.role]}
                  >
                    <option value="member">member</option>
                    <option value="admin">bot admin</option>
                  </select>
                ) : (
                  <span className="text-[10px] text-zinc-400" title={ROLE_HELP[member.role]}>{member.role === 'admin' ? 'bot admin' : 'member'}</span>
                )}
                {canRemove && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => removeBotMember(bot.id, member.user_id), 'Could not remove them.')}
                    className="p-1 rounded-md text-zinc-500 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition"
                    title={`Stop sharing with ${member.name}`}
                  >
                    <FiX className="text-[11px]" />
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>

      {manages && (
        <div className="flex flex-wrap items-center gap-2">
          {candidates.length > 0 ? (
            <select value={username} onChange={(e) => setUsername(e.target.value)} className={`${inputClass} flex-1 min-w-[10rem]`}>
              <option value="">Share with…</option>
              {candidates.map((p) => (
                <option key={p.user_id} value={p.username}>
                  {p.name === p.username ? p.username : `${p.name} (@${p.username})`}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-[11px] text-zinc-500 flex-1">Everyone on this server already has it, or there is nobody else yet.</span>
          )}
          {myRole === 'owner' && candidates.length > 0 && (
            <select value={role} onChange={(e) => setRole(e.target.value)} className={inputClass} title={ROLE_HELP[role]}>
              <option value="member">member</option>
              <option value="admin">bot admin</option>
            </select>
          )}
          {candidates.length > 0 && (
            <button
              type="button"
              disabled={busy || !username}
              onClick={async () => {
                if (await run(() => addBotMember(bot.id, username, myRole === 'owner' ? role : 'member'), 'Could not share the bot.')) setUsername('');
              }}
              className="px-3 py-2 rounded-xl text-xs font-medium bg-zinc-100 text-black hover:bg-white disabled:opacity-40 transition flex items-center gap-1.5"
            >
              <FiUserPlus className="text-xs" /> Share
            </button>
          )}
        </div>
      )}

      {myRole !== 'owner' && onLeave && (
        <button
          type="button"
          disabled={busy}
          onClick={() => onLeave(bot)}
          className="px-3 py-1.5 rounded-lg text-[11px] text-rose-400 hover:text-rose-300 hover:bg-rose-500/10 disabled:opacity-50 transition"
        >
          Leave this bot
        </button>
      )}

      {error && <p className="text-[11px] text-rose-400">{error}</p>}
      <p className="text-[10px] leading-relaxed text-zinc-500">
        Everyone it is shared with uses the same bot, computer, files and notes, and their conversations with it are their own: nobody, the owner included,
        sees anyone else&apos;s. Their conversations do not get the owner&apos;s connected apps, own MCP servers or account-wide secrets; the bot&apos;s own
        secrets and MCP servers are shared with it.
      </p>
    </div>
  );
}
