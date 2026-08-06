import { useState, useEffect } from 'react';
import { 
    Stack, Button, Slider, Text, Switch, MultiSelect, 
    NumberInput, TextInput, Select, Paper, Collapse, Alert, Group, Tooltip
} from '@mantine/core';
import { useForm } from '@mantine/form';
import { IconAlertCircle, IconInfoCircle } from '@tabler/icons-react';
import { MoleculeRequest, getReactionTags, getBuildingBlocks, getServerLimits, BuildingBlockOption, ServerLimits } from '../api';

const DEFAULT_REACTION_TAGS = [
    "amide coupling", "amide", "C-N bond formation", "C-N",
    "alkylation", "N-arylation", "azole", "amination"
];

const LabelWithTooltip = ({ label, tooltip }: { label: string, tooltip: string }) => (
    <Group gap={5}>
        <Text size="sm" fw={500}>{label}</Text>
        <Tooltip label={tooltip} multiline w={220} withArrow>
            <IconInfoCircle size={14} style={{ cursor: 'help', color: 'var(--mantine-color-dimmed)' }} />
        </Tooltip>
    </Group>
);

interface MoleculeFormProps {
    onSubmit: (values: Omit<MoleculeRequest, 'molecule'>) => void;
    isLoading: boolean;
    isMultiFragment: boolean; // From parent detecting '.' in SMILES
}

export function MoleculeForm({ onSubmit, isLoading, isMultiFragment }: MoleculeFormProps) {
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [reactionTags, setReactionTags] = useState<string[]>(DEFAULT_REACTION_TAGS);
    const [bbSources, setBbSources] = useState<BuildingBlockOption[]>([
        { value: 'test_100_bb_processed.sdf', label: 'Test Set (100 BBs)' }
    ]);
    const [serverLimits, setServerLimits] = useState<ServerLimits | null>(null);
    const [isServerMode, setIsServerMode] = useState(false);

    useEffect(() => {
        getReactionTags().then(tags => {
            if (tags && tags.length > 0) setReactionTags(tags);
        }).catch(err => console.error("Failed to fetch reaction tags", err));
        
        getBuildingBlocks().then(bbs => {
            if (bbs && bbs.length > 0) setBbSources(bbs);
        }).catch(err => console.error("Failed to fetch building blocks", err));
        
        getServerLimits().then(res => {
            setServerLimits(res.limits);
            setIsServerMode(res.server_mode);
        }).catch(err => console.error("Failed to fetch server limits", err));
    }, []);
    
    const form = useForm({
        initialValues: {
            bb_source: 'test_100_bb_processed.sdf',
            reaction_tags: DEFAULT_REACTION_TAGS as string[],
            sim_threshold: 0.50,
            n_compositions: 10,
            randomize_compositions: false,
            random_seed: -1,
            retro_tree_depth: 1,
            min_frag_size: 7,
            max_bbs_per_frag: 10,
            shuffle_bb_order: false,
            max_evals_per_comp: 1000,
            max_products_per_comp: 100,
            max_total_products: 1000,
            custom_sites_str: '',
        },
    });

    // Update bb_source when bbSources changes (set first available)
    useEffect(() => {
        if (bbSources.length > 0 && !bbSources.find(b => b.value === form.values.bb_source)) {
            form.setFieldValue('bb_source', bbSources[0].value);
        }
    }, [bbSources]);

    const handleSubmit = (values: typeof form.values) => {
        // Parse custom sites string "1-2, 3-4" -> [[1,2], [3,4]]
        let custom_sites: [number, number][] | undefined = undefined;
        if (values.custom_sites_str && values.custom_sites_str.trim()) {
            try {
                 const pairs = values.custom_sites_str.split(/[;,]+/).map(s => s.trim());
                 const parsedPairs = pairs.map(p => {
                     const parts = p.split('-').map(n => parseInt(n.trim()));
                     if (parts.length === 2 && !isNaN(parts[0]) && !isNaN(parts[1])) {
                         return parts as [number, number];
                     }
                     return null;
                 }).filter((p): p is [number, number] => p !== null);
                 if (parsedPairs.length > 0) custom_sites = parsedPairs;
            } catch (e) {
                console.error("Failed to parse custom sites", e);
            }
        }

        const payload: Omit<MoleculeRequest, 'molecule'> = {
            bb_source: values.bb_source,
            reaction_tags: values.reaction_tags,
            sim_threshold: values.sim_threshold,
            n_compositions: values.n_compositions,
            randomize_compositions: values.randomize_compositions,
            random_seed: values.random_seed,
            retro_tree_depth: values.retro_tree_depth,
            min_frag_size: values.min_frag_size,
            max_bbs_per_frag: values.max_bbs_per_frag,
            shuffle_bb_order: values.shuffle_bb_order,
            max_evals_per_comp: values.max_evals_per_comp,
            max_products_per_comp: values.max_products_per_comp,
            max_total_products: values.max_total_products,
            use_fragment_healer: isMultiFragment,
            custom_sites
        };
        onSubmit(payload);
    };

    return (
        <form onSubmit={form.onSubmit(handleSubmit)}>
            <Stack gap="md">
                <Text size="sm" c="dimmed">
                    Enumerate libraries starting from a whole molecule by retrosynthetically fragmenting it. 
                    You can also provide a multi-fragment molecule to use <b>Fragment HEALER</b>.
                </Text>
                {isMultiFragment && (
                    <Alert icon={<IconAlertCircle size={16} />} title="Fragment Mode Detected" color="blue">
                         Multi-component molecule detected. System will use Fragment HEALER.
                    </Alert>
                )}

                <Select
                    label={<LabelWithTooltip label="Building Block Source" tooltip="Choose the library of building blocks to use for enumeration." />}
                    data={bbSources}
                    {...form.getInputProps('bb_source')}
                />

                <MultiSelect
                    label={<LabelWithTooltip label="Reaction Tags" tooltip={`Select reaction tags to specify the reaction chemistry. One tag may point to multiple reaction.${isServerMode && serverLimits ? ` Server max: ${serverLimits.max_reaction_tags}` : ''}`} />}
                    data={reactionTags}
                    searchable
                    maxValues={isServerMode && serverLimits ? serverLimits.max_reaction_tags : 20}
                    {...form.getInputProps('reaction_tags')}
                />

                <Paper withBorder p="md" bg="var(--mantine-color-gray-0)">
                    <Group justify="space-between" mb="xs">
                         <LabelWithTooltip 
                            label={`Similarity Threshold: ${form.values.sim_threshold}`} 
                            tooltip={`Minimum Tanimoto similarity required between a fragment and a building block.${isServerMode && serverLimits ? ` Server limit: ${serverLimits.sim_threshold_min}-${serverLimits.sim_threshold_max}` : ''}`} 
                         />
                    </Group>
                    <Slider 
                        min={isServerMode && serverLimits ? serverLimits.sim_threshold_min : 0} 
                        max={isServerMode && serverLimits ? serverLimits.sim_threshold_max : 1} 
                        step={0.01} 
                        label={val => val.toFixed(2)}
                        {...form.getInputProps('sim_threshold')}
                    />
                </Paper>

                <Button variant="subtle" size="xs" onClick={() => setShowAdvanced(!showAdvanced)}>
                    {showAdvanced ? 'Hide Advanced Options' : 'Show Advanced Options'}
                </Button>

                <Collapse in={showAdvanced}>
                    <Stack gap="md">
                        <TextInput
                            label={<LabelWithTooltip label="Split Sites" tooltip="Manually specify bonds to break using atom indices (e.g., '1-2, 3-4'). This will disable the retrosynthesis." />}
                            placeholder="e.g., 9-10, 6-9"
                            {...form.getInputProps('custom_sites_str')}
                        />
                        <Group grow align="flex-start">
                            <Group grow align="center" gap={4}>
                                <LabelWithTooltip label="Shuffle Compositions" tooltip="Shuffle the order of fragment compositions." />
                                <Switch 
                                    {...form.getInputProps('randomize_compositions', { type: 'checkbox' })}
                                />
                            </Group>
                            <Group grow align="center" gap={4}>
                                <LabelWithTooltip label="Shuffle BB Order" tooltip="Shuffle the order of building blocks within the library." />
                                <Switch 
                                    {...form.getInputProps('shuffle_bb_order', { type: 'checkbox' })}
                                />
                            </Group>
                        </Group>
                        <Group grow>
                            <NumberInput
                                label={<LabelWithTooltip label="N Compositions" tooltip={`Number of different fragment compositions to explore.${isServerMode && serverLimits ? ` Server max: ${serverLimits.n_compositions_max}` : ''}`} />}
                                min={1} 
                                max={isServerMode && serverLimits ? serverLimits.n_compositions_max : 100}
                                {...form.getInputProps('n_compositions')}
                            />
                            <NumberInput
                                label={<LabelWithTooltip label="Max BBs per Fragment" tooltip={`Use top N building blocks per fragment instead of similarity threshold.${isServerMode && serverLimits ? ` Server range: 1–${serverLimits.max_bbs_per_frag}` : ' Set 0 to disable.'}`} />}
                                min={isServerMode ? 1 : 0}
                                max={isServerMode && serverLimits ? serverLimits.max_bbs_per_frag : 100}
                                {...form.getInputProps('max_bbs_per_frag')}
                            />
                        </Group>
                        <NumberInput
                            label={<LabelWithTooltip label="Max Evals / Comp" tooltip={`Maximum number of reaction attempts per composition.${isServerMode && serverLimits ? ` Server max: ${serverLimits.max_evals_per_comp}` : ''}`} />}
                            min={1} 
                            max={isServerMode && serverLimits ? serverLimits.max_evals_per_comp : undefined}
                            {...form.getInputProps('max_evals_per_comp')}
                        />
                        <Group grow>
                            <NumberInput
                                label={<LabelWithTooltip label="Max Products / Comp" tooltip={`Maximum number of products to generate per composition.${isServerMode && serverLimits ? ` Server max: ${serverLimits.max_products_per_comp}` : ''}`} />}
                                min={1}
                                max={isServerMode && serverLimits ? serverLimits.max_products_per_comp : undefined}
                                placeholder="Unlimited"
                                {...form.getInputProps('max_products_per_comp')}
                            />
                            <NumberInput
                                label={<LabelWithTooltip label="Max Total Products" tooltip={`Stop enumeration after generating this many products total.${isServerMode && serverLimits ? ` Server max: ${serverLimits.max_total_products}` : ''}`} />}
                                min={1}
                                max={isServerMode && serverLimits ? serverLimits.max_total_products : undefined}
                                placeholder="Unlimited"
                                {...form.getInputProps('max_total_products')}
                            />
                        </Group>
                        <Group grow>
                            <NumberInput 
                                label={<LabelWithTooltip label="Retro Depth" tooltip={`Depth of the retrosynthetic tree search.${isServerMode && serverLimits ? ` Server max: ${serverLimits.retro_depth_max}` : ''}`} />}
                                min={1} 
                                max={isServerMode && serverLimits ? serverLimits.retro_depth_max : 5} 
                                {...form.getInputProps('retro_tree_depth')}
                            />
                            <NumberInput 
                                label={<LabelWithTooltip label="Min Frag Size" tooltip={`Minimum number of heavy atoms for a fragment to be valid.${isServerMode && serverLimits ? ` Server min: ${serverLimits.min_frag_size_min}` : ''}`} />}
                                min={isServerMode && serverLimits ? serverLimits.min_frag_size_min : 1} 
                                max={15} 
                                {...form.getInputProps('min_frag_size')}
                            />
                        </Group>
                    </Stack>
                </Collapse>

                <Button type="submit" loading={isLoading} fullWidth size="md">
                    Enumerate
                </Button>
            </Stack>
        </form>
    );
}
