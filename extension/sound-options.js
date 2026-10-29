export const SOUND_EFFECTS = [
    {name: 'Message', file: 'sound_effect_1.mp3'},
    {name: 'Level Up 07', file: 'sound_effect_2.mp3'},
    {name: 'Notification Chime', file: 'sound_effect_3.mp3'},
];

export const SOUND_REPEATS = [
    {id: 'once', name: 'Once', seconds: 0},
    {id: 'minute', name: 'Every minute', seconds: 60},
    {id: 'five-minutes', name: 'Every five minutes', seconds: 300},
];

export function soundFile(effect, customFile, extensionPath) {
    if (customFile) return customFile;
    const selected = SOUND_EFFECTS[effect - 1] ?? SOUND_EFFECTS[0];
    return `${extensionPath}/sounds/${selected.file}`;
}
