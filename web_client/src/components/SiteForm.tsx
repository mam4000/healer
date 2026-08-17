import { useState, useEffect } from 'react';
import { 
    Stack, Button, Text, Switch, MultiSelect, 
    NumberInput, TextInput, Select, Paper, Collapse, Group, RangeSlider, Tooltip
} from '@mantine/core';
import { useForm } from '@mantine/form';
import { IconInfoCircle } from '@tabler/icons-react';
import { SiteRequest, getReactionTags, getBuildingBlocks, getServerLimits, BuildingBlockOption, ServerLimits } from '../api';

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

interface SiteFormProps {
    onSubmit: (values: Omit<SiteRequest, 'molecule'>) => void;
    isLoading: boolean;
}

export function SiteForm({ onSubmit, isLoading }: SiteFormProps) {
    const [showFilters, setShowFilters] = useState(false);
    const [showAdvanced, setShowAdvanced] = useState(false);
    const [reactionTags, setReactionTags] = useState<string[]>(DEFAULT_REACTION_TAGS);
    const [bbSources, setBbSources] = useState<BuildingBlockOption[]>([
        { value: 'molport_full', label: 'Molport Full Database' }
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
            bb_source: 'molport_full',
            reaction_tags: DEFAULT_REACTION_TAGS,
            reactive_sites_str: '',
            struct_rules_str: '',
            shuffle_bb_order: false,
            max_evals_per_comp: 10000,
            max_products_per_comp: 1000,
            max_total_products: 1000,
            
            // Property Rules
            rule_MW: [0, 250] as [number, number],
            rule_HBD: [0, 2] as [number, number],
            rule_HBA: [0, 5] as [number, number],
            rule_TPSA: [0, 60] as [number, number],
            rule_RotB: [0, 5] as [number, number],
            rule_Rings: [0, 3] as [number, number],
            rule_ArRings: [0, 1] as [number, number],
            rule_Chiral: [0, 1] as [number, number],
        },
    });

    // Update bb_source when bbSources changes
    useEffect(() => {
        if (bbSources.length > 0 && !bbSources.find(b => b.value === form.values.bb_source)) {
            form.setFieldValue('bb_source', bbSources[0].value);
        }
    }, [bbSources]);

    useEffect(() => {
        form.setFieldValue('reaction_tags', DEFAULT_REACTION_TAGS);
    }, []);

    const handleSubmit = (values: typeof form.values) => {
        // Parse reactive sites "1, 2, 5" -> [1, 2, 5]
        let reactive_sites: number[] | undefined = undefined;
        if (values.reactive_sites_str && values.reactive_sites_str.trim()) {
             reactive_sites = values.reactive_sites_str.split(',')
                .map(s => parseInt(s.trim()))
                .filter(n => !isNaN(n));
             if (reactive_sites.length === 0) reactive_sites = undefined;
        }

        // Parse struct rules (SMARTS)
        let struct_rules: string[] | undefined = undefined;
        if (values.struct_rules_str && values.struct_rules_str.trim()) {
            struct_rules = values.struct_rules_str.split(',').map(s => s.trim()).filter(s => s.length > 0);
        }

        const rules: Record<string, [number, number]> = {
            'MW': values.rule_MW,
            'HBD': values.rule_HBD,
            'HBA': values.rule_HBA,
            'TPSA': values.rule_TPSA,
            'RotB': values.rule_RotB,
            'Rings': values.rule_Rings,
            'ArRings': values.rule_ArRings,
            'Chiral': values.rule_Chiral,
        };

        const payload: Omit<SiteRequest, 'molecule'> = {
            bb_source: values.bb_source,
            reaction_tags: values.reaction_tags,
            reactive_sites,
            rules,
            struct_rules,
            shuffle_bb_order: values.shuffle_bb_order,
            max_evals_per_comp: values.max_evals_per_comp,
            max_products_per_comp: values.max_products_per_comp,
            max_total_products: values.max_total_products,
        };
        onSubmit(payload);
    };

    const renderFilterSlider = (label: string, field: keyof typeof form.values, min: number, max: number, step: number = 1) => {
        const value = form.values[field] as [number, number];
        return (
            <Paper withBorder p="xs" bg="var(--mantine-color-gray-0)">
                <Text size="xs" fw={500} mb={4}>{label}: {value[0]} - {value[1]}</Text>
                <RangeSlider
                    min={min} max={max} step={step} minRange={0}
                    {...form.getInputProps(field)}
                />
            </Paper>
        );
    };

    return (
        <form onSubmit={form.onSubmit(handleSubmit)}>
            <Stack gap="md">
                <Text size="sm" c="dimmed">
                    Enumerate libraries by attaching building blocks to specific reactive sites on the molecule.
                    Use the filters below to restrict building blocks based on their properties. You can also set 
                    substructure rules using SMARTS patterns to further refine the selection.
                </Text>

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
                
                <TextInput
                    label={<LabelWithTooltip label="Reactive Sites (Indices)" tooltip="Comma-separated atom indices to target (e.g., '1, 5'). Leave empty for all sites." />}
                    placeholder="e.g., 1, 5 (Leave empty for all)"
                    {...form.getInputProps('reactive_sites_str')}
                />

                <Button variant="subtle" size="xs" onClick={() => setShowFilters(!showFilters)}>
                    {showFilters ? 'Hide BB Filters' : 'Show BB Filters (Properties)'}
                </Button>

                <Collapse in={showFilters}>
                    <Stack gap="xs">
                        {renderFilterSlider('Molecular Weight', 'rule_MW', 0, 500, 10)}
                        {renderFilterSlider('H-Bond Donors', 'rule_HBD', 0, 5)}
                        {renderFilterSlider('H-Bond Acceptors', 'rule_HBA', 0, 10)}
                        {renderFilterSlider('TPSA', 'rule_TPSA', 0, 120, 5)}
                        {renderFilterSlider('Rotatable Bonds', 'rule_RotB', 0, 10)}
                        {renderFilterSlider('Total Rings', 'rule_Rings', 0, 5)}
                        {renderFilterSlider('Aromatic Rings', 'rule_ArRings', 0, 5)}
                        {renderFilterSlider('Chiral Centers', 'rule_Chiral', 0, 5)}
                    </Stack>
                </Collapse>

                <Button variant="subtle" size="xs" onClick={() => setShowAdvanced(!showAdvanced)}>
                    {showAdvanced ? 'Hide Advanced Options' : 'Show Advanced Options'}
                </Button>

                <Collapse in={showAdvanced}>
                    <Stack gap="md">
                         <TextInput
                            label={<LabelWithTooltip label="Structure Rules (SMARTS)" tooltip="Comma-separated SMARTS patterns that building blocks must contain." />}
                            placeholder="e.g., c1ccccc1, [CX3](=O)[OX2H1]"
                            {...form.getInputProps('struct_rules_str')}
                        />
                        <Switch 
                            label={<LabelWithTooltip label="Shuffle BB Order" tooltip="Randomize the order of building blocks within the library." />}
                            {...form.getInputProps('shuffle_bb_order', { type: 'checkbox' })}
                        />
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
                    </Stack>
                </Collapse>

                <Button type="submit" loading={isLoading} fullWidth size="md" color="violet">
                    Enumerate Sites
                </Button>
            </Stack>
        </form>
    );
}
